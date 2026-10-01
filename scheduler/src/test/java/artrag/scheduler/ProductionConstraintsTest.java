package artrag.scheduler;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

import ai.timefold.solver.core.api.score.stream.test.ConstraintVerifier;
import ai.timefold.solver.core.api.solver.SolverFactory;
import ai.timefold.solver.core.config.solver.SolverConfig;

import org.junit.jupiter.api.Test;

import artrag.scheduler.SolverService.JobDto;
import artrag.scheduler.SolverService.MachineDto;
import artrag.scheduler.SolverService.OperationDto;
import artrag.scheduler.SolverService.ProblemRequest;
import artrag.scheduler.domain.Machine;
import artrag.scheduler.domain.Operation;
import artrag.scheduler.domain.ProductionSchedule;
import artrag.scheduler.solver.ProductionConstraints;

class ProductionConstraintsTest {

    private final ConstraintVerifier<ProductionConstraints, ProductionSchedule> verifier = ConstraintVerifier.build(
            new ProductionConstraints(), ProductionSchedule.class, Machine.class, Operation.class);

    /** 車床 L1 先做 A#10 再做 B#10；綜合加工機 V1 做 A#20（之後委外 30 分鐘） */
    private static ProductionSchedule sample() {
        var req = new ProblemRequest("t", 1, 0,
                List.of(new MachineDto("L1", "車削"), new MachineDto("L2", "車削"), new MachineDto("V1", "綜合加工")),
                List.of(new JobDto("A", 0, 100, 1), new JobDto("B", 0, 60, 3)),
                List.of(new OperationDto("A#10", "A", 10, "車削", 10, 50, 0, "p1#10", null),
                        new OperationDto("A#20", "A", 20, "綜合加工", 5, 20, 30, "p1#20", null),
                        new OperationDto("B#10", "B", 10, "車削", 10, 40, 0, "p2#10", null)));
        var s = SolverService.build(req);
        var ops = s.getOperations();
        var l1 = s.getMachines().get(0);
        var v1 = s.getMachines().get(2);
        l1.setOperations(new ArrayList<>(List.of(ops.get(0), ops.get(2))));
        v1.setOperations(new ArrayList<>(List.of(ops.get(1))));
        return s;
    }

    @Test
    void tardinessIsWeightedLateMinutes() {
        // B：0–60 做 A#10，60–110 做 B#10 → 晚 50 分鐘 × 權重 3；A：A#20 60–85＋委外 30＝115 → 晚 15 分鐘
        verifier.verifyThat(ProductionConstraints::tardiness)
                .givenSolution(sample())
                .settingAllShadowVariables()
                .penalizesBy(50 * 3 + 15);
    }

    @Test
    void setupAndCompletion() {
        verifier.verifyThat(ProductionConstraints::setupTime)
                .givenSolution(sample())
                .settingAllShadowVariables()
                .penalizesBy(10 + 10 + 5);
        verifier.verifyThat(ProductionConstraints::completionTime)
                .givenSolution(sample())
                .settingAllShadowVariables()
                .penalizesBy(110 + 115);
    }

    @Test
    void sameFamilyBackToBackSkipsSetup() {
        var req = new ProblemRequest("t", 1, 0, List.of(new MachineDto("L1", "車削")),
                List.of(new JobDto("A", 0, 1000, 1), new JobDto("B", 0, 1000, 1)),
                List.of(new OperationDto("A#10", "A", 10, "車削", 30, 50, 0, "p1#10", null),
                        new OperationDto("B#10", "B", 10, "車削", 30, 40, 0, "p1#10", null)));
        var s = SolverService.build(req);
        s.getMachines().get(0).setOperations(new ArrayList<>(s.getOperations()));
        verifier.verifyThat(ProductionConstraints::setupTime)
                .givenSolution(s)
                .settingAllShadowVariables()
                .penalizesBy(30);
    }

    @Test
    void solverAssignsEveryOperationAndKeepsPinnedFirst() {
        var req = new ProblemRequest("t", 1, 0,
                List.of(new MachineDto("L1", "車削"), new MachineDto("L2", "車削"), new MachineDto("V1", "綜合加工")),
                List.of(new JobDto("A", 0, 2000, 1), new JobDto("B", 0, 300, 3), new JobDto("C", 0, 900, 1)),
                List.of(new OperationDto("A#10", "A", 10, "車削", 0, 120, 0, "p1#10", "L1"),
                        new OperationDto("A#20", "A", 20, "綜合加工", 20, 60, 480, "p1#20", null),
                        new OperationDto("B#10", "B", 10, "車削", 30, 90, 0, "p2#10", null),
                        new OperationDto("B#20", "B", 20, "綜合加工", 20, 60, 0, "p2#20", null),
                        new OperationDto("C#10", "C", 10, "車削", 30, 200, 0, "p3#10", null)));
        var config = new SolverConfig()
                .withSolutionClass(ProductionSchedule.class)
                .withEntityClasses(Machine.class, Operation.class)
                .withConstraintProviderClass(ProductionConstraints.class)
                .withTerminationSpentLimit(Duration.ofSeconds(1));
        var best = SolverFactory.<ProductionSchedule>create(config).buildSolver().solve(SolverService.build(req));
        assertTrue(best.getScore().isFeasible(), best.getScore().toString());
        assertEquals(5, best.getMachines().stream().mapToInt(m -> m.getOperations().size()).sum());
        assertEquals("A#10", best.getMachines().get(0).getOperations().get(0).getId());
        for (var op : best.getOperations()) {
            assertEquals(op.getMachineType(), op.getMachine().getType());
            if (op.getPreviousInJob() != null) {
                var prev = op.getPreviousInJob();
                assertTrue(op.getStartMin() >= prev.getEndMin() + prev.getLagAfterMin(), op + " 早於前一道工序");
            }
        }
        // 急件 B 可以在交期前完工：L2 做 B#10、V1 先做 B#20
        assertEquals(0, best.getScore().mediumScore(), best.getScore().toString());
    }
}
