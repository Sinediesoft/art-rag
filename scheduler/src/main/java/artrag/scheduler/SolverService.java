package artrag.scheduler;

import java.time.Duration;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

import ai.timefold.solver.core.api.score.HardMediumSoftScore;
import ai.timefold.solver.core.api.solver.SolverConfigOverride;
import ai.timefold.solver.core.api.solver.SolverFactory;
import ai.timefold.solver.core.api.solver.SolverManager;
import ai.timefold.solver.core.config.solver.SolverConfig;
import ai.timefold.solver.core.config.solver.SolverManagerConfig;
import ai.timefold.solver.core.config.solver.termination.TerminationConfig;

import artrag.scheduler.domain.Machine;
import artrag.scheduler.domain.Operation;
import artrag.scheduler.domain.ProductionSchedule;
import artrag.scheduler.domain.WorkOrder;
import artrag.scheduler.solver.ProductionConstraints;

/**
 * 管理求解工作：建問題 → SolverManager 背景求解 → 每次找到更好的解就更新快照，給後端輪詢。
 * <p>
 * 各限制條件的分數明細（Score analysis）在 Timefold 2.x 屬商業版功能，這裡只回傳總分；
 * 明細由後端用同一套規則重算（app/scheduling/solution.py），並核對總分與這裡一致。
 */
public class SolverService {

    public record MachineDto(String id, String type) {
    }

    public record JobDto(String id, long releaseMin, long dueMin, int weight) {
    }

    public record OperationDto(String id, String jobId, int seq, String machineType, long setupMin, long runMin,
            long lagAfterMin, String setupFamily, String pinnedMachineId) {
    }

    public record ProblemRequest(String problemId, int spentLimitSeconds, int unimprovedSpentLimitSeconds,
            List<MachineDto> machines, List<JobDto> jobs, List<OperationDto> operations) {
    }

    /** 一次求解的狀態；求解執行緒寫入、HTTP 執行緒讀取 */
    static final class JobState {
        final String id;
        final String problemId;
        final long startedNanos = System.nanoTime();
        volatile String status = "SOLVING";
        volatile String phase = "建構解";
        volatile HardMediumSoftScore bestScore;
        volatile HardMediumSoftScore initialScore;
        volatile int improvements;
        volatile long bestAtMs;
        volatile long endedMs = -1;
        volatile boolean stopRequested;
        volatile List<Map<String, Object>> assignments = List.of();
        volatile String error;

        JobState(String id, String problemId) {
            this.id = id;
            this.problemId = problemId;
        }

        long elapsedMs() {
            return (System.nanoTime() - startedNanos) / 1_000_000;
        }
    }

    private final SolverManager<ProductionSchedule> solverManager;
    private final Map<String, JobState> jobs = new ConcurrentHashMap<>();

    public SolverService() {
        var config = new SolverConfig()
                .withSolutionClass(ProductionSchedule.class)
                .withEntityClasses(Machine.class, Operation.class)
                .withConstraintProviderClass(ProductionConstraints.class)
                .withTerminationSpentLimit(Duration.ofSeconds(30));
        SolverFactory<ProductionSchedule> factory = SolverFactory.create(config);
        solverManager = SolverManager.create(factory, new SolverManagerConfig().withParallelSolverCount("1"));
    }

    // ************************************************************************
    // 建問題
    // ************************************************************************

    static ProductionSchedule build(ProblemRequest req) {
        var workOrders = new LinkedHashMap<String, WorkOrder>();
        for (var j : req.jobs()) {
            workOrders.put(j.id(), new WorkOrder(j.id(), j.releaseMin(), j.dueMin(), Math.max(1, j.weight())));
        }
        var machines = new LinkedHashMap<String, Machine>();
        for (var m : req.machines()) {
            machines.put(m.id(), new Machine(m.id(), m.type()));
        }
        var operations = new ArrayList<Operation>();
        var byJob = new HashMap<String, List<Operation>>();
        for (var o : req.operations()) {
            var wo = workOrders.get(o.jobId());
            if (wo == null) {
                throw new IllegalArgumentException("工序 " + o.id() + " 的工單 " + o.jobId() + " 不存在");
            }
            if (machines.values().stream().noneMatch(m -> m.getType().equals(o.machineType()))) {
                throw new IllegalArgumentException("工序 " + o.id() + " 的機型 " + o.machineType() + " 沒有任何機台");
            }
            var op = new Operation(o.id(), wo, o.seq(), o.machineType(), o.setupMin(), o.runMin(), o.lagAfterMin(),
                    o.setupFamily(), o.pinnedMachineId() != null);
            operations.add(op);
            byJob.computeIfAbsent(o.jobId(), k -> new ArrayList<>()).add(op);
            if (o.pinnedMachineId() != null) {
                var m = machines.get(o.pinnedMachineId());
                if (m == null || !m.getType().equals(o.machineType())) {
                    throw new IllegalArgumentException("工序 " + o.id() + " 釘選的機台 " + o.pinnedMachineId() + " 不存在或機型不符");
                }
                m.getOperations().add(op);
                m.setFirstUnpinnedIndex(m.getOperations().size());
            }
        }
        for (var ops : byJob.values()) {
            ops.sort(Comparator.comparingInt(Operation::getSeq));
            for (int i = 0; i < ops.size(); i++) {
                ops.get(i).setPreviousInJob(i == 0 ? null : ops.get(i - 1));
                ops.get(i).setLastInJob(i == ops.size() - 1);
            }
        }
        for (var m : machines.values()) {
            m.setAllowedOperations(operations.stream().filter(op -> op.getMachineType().equals(m.getType())).toList());
        }
        return new ProductionSchedule(new ArrayList<>(workOrders.values()), new ArrayList<>(machines.values()), operations);
    }

    static List<Map<String, Object>> assignments(ProductionSchedule s) {
        var out = new ArrayList<Map<String, Object>>();
        for (var m : s.getMachines()) {
            for (var op : m.getOperations()) {
                var row = new LinkedHashMap<String, Object>();
                row.put("operationId", op.getId());
                row.put("machineId", m.getId());
                row.put("startMin", op.getStartMin());
                row.put("endMin", op.getEndMin());
                row.put("setupMin", op.getSetupApplied());
                row.put("pinned", op.isPinned());
                out.add(row);
            }
        }
        return out;
    }

    static Map<String, Object> scoreMap(HardMediumSoftScore score) {
        var out = new LinkedHashMap<String, Object>();
        if (score == null) {
            return out;
        }
        out.put("score", score.toString());
        out.put("structural", score.structuralScore());
        out.put("hard", score.hardScore());
        out.put("medium", score.mediumScore());
        out.put("soft", score.softScore());
        out.put("feasible", score.isFeasible());
        return out;
    }

    // ************************************************************************
    // 求解
    // ************************************************************************

    public String submit(ProblemRequest req) {
        var problem = build(req);
        var id = "tf_" + UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        var state = new JobState(id, req.problemId());
        jobs.put(id, state);
        trim();
        var termination = new TerminationConfig()
                .withSpentLimit(Duration.ofSeconds(Math.clamp(req.spentLimitSeconds(), 1, 300)));
        if (req.unimprovedSpentLimitSeconds() > 0) {
            termination.setUnimprovedSpentLimit(Duration.ofSeconds(req.unimprovedSpentLimitSeconds()));
        }
        solverManager.solveBuilder()
                .withProblemId(id)
                .withProblem(problem)
                .withConfigOverride(new SolverConfigOverride().withTerminationConfig(termination))
                .withFirstInitializedSolutionEventConsumer(event -> {
                    state.initialScore = event.solution().getScore();
                    state.phase = "局部搜尋";
                })
                .withBestSolutionEventConsumer(event -> {
                    var solution = event.solution();
                    state.assignments = assignments(solution);
                    state.bestScore = solution.getScore();
                    state.improvements++;
                    state.bestAtMs = state.elapsedMs();
                    var producer = event.producerId().simpleProducerName();
                    if (producer.contains("Construction")) {
                        state.phase = "建構解";
                    } else if (producer.contains("Local Search")) {
                        state.phase = "局部搜尋";
                    }
                })
                .withFinalBestSolutionEventConsumer(event -> {
                    var solution = event.solution();
                    state.assignments = assignments(solution);
                    state.bestScore = solution.getScore();
                    state.endedMs = state.elapsedMs();
                    state.status = state.stopRequested ? "STOPPED" : "DONE";
                })
                .withExceptionHandler((problemId, throwable) -> {
                    state.error = throwable.getClass().getSimpleName() + ": " + throwable.getMessage();
                    state.endedMs = state.elapsedMs();
                    state.status = "FAILED";
                })
                .run();
        return id;
    }

    public boolean stop(String jobId) {
        var state = jobs.get(jobId);
        if (state == null) {
            return false;
        }
        state.stopRequested = true;
        solverManager.terminateEarly(jobId);
        return true;
    }

    public Map<String, Object> status(String jobId, boolean withSolution) {
        var s = jobs.get(jobId);
        if (s == null) {
            return null;
        }
        var out = new LinkedHashMap<String, Object>();
        out.put("jobId", s.id);
        out.put("problemId", s.problemId);
        out.put("status", s.status);
        out.put("phase", s.phase);
        out.put("elapsedMs", s.endedMs >= 0 ? s.endedMs : s.elapsedMs());
        out.put("bestAtMs", s.bestAtMs);
        out.put("improvements", s.improvements);
        out.putAll(scoreMap(s.bestScore));
        out.put("initialScore", s.initialScore == null ? null : s.initialScore.toString());
        out.put("error", s.error);
        if (withSolution) {
            out.put("assignments", s.assignments);
        }
        return out;
    }

    public int activeJobs() {
        return (int) jobs.values().stream().filter(s -> "SOLVING".equals(s.status)).count();
    }

    /** 只留最近幾次的結果，避免長時間執行時記憶體一直長 */
    private void trim() {
        var finished = jobs.values().stream()
                .filter(s -> !"SOLVING".equals(s.status))
                .sorted(Comparator.comparingLong(s -> s.startedNanos))
                .toList();
        for (int i = 0; i < finished.size() - 4; i++) {
            jobs.remove(finished.get(i).id);
        }
    }

    /** 記憶體吃緊時由後端呼叫：清掉已結束的求解結果並 GC，讓 JVM 把閒置的 heap 還給作業系統 */
    public int release() {
        int before = jobs.size();
        jobs.values().removeIf(s -> !"SOLVING".equals(s.status));
        System.gc();
        return before - jobs.size();
    }

    public void close() {
        solverManager.close();
    }
}
