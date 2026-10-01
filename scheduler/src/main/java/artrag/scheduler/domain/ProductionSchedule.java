package artrag.scheduler.domain;

import java.util.List;

import ai.timefold.solver.core.api.domain.solution.PlanningEntityCollectionProperty;
import ai.timefold.solver.core.api.domain.solution.PlanningScore;
import ai.timefold.solver.core.api.domain.solution.PlanningSolution;
import ai.timefold.solver.core.api.domain.solution.ProblemFactCollectionProperty;
import ai.timefold.solver.core.api.score.HardMediumSoftScore;

/**
 * 排程問題與解（planning solution）：工單是事實、機台與工序是規劃實體。
 * <p>
 * 分數分三級（數字越接近 0 越好）：硬＝不可行（機型不符）、中＝交期延遲、軟＝換線準備＋完工時間。
 */
@PlanningSolution
public class ProductionSchedule {

    @ProblemFactCollectionProperty
    private List<WorkOrder> workOrders;

    @PlanningEntityCollectionProperty
    private List<Machine> machines;

    @PlanningEntityCollectionProperty
    private List<Operation> operations;

    @PlanningScore
    private HardMediumSoftScore score;

    public ProductionSchedule() {
    }

    public ProductionSchedule(List<WorkOrder> workOrders, List<Machine> machines, List<Operation> operations) {
        this.workOrders = workOrders;
        this.machines = machines;
        this.operations = operations;
    }

    public List<WorkOrder> getWorkOrders() {
        return workOrders;
    }

    public List<Machine> getMachines() {
        return machines;
    }

    public List<Operation> getOperations() {
        return operations;
    }

    public HardMediumSoftScore getScore() {
        return score;
    }

    public void setScore(HardMediumSoftScore score) {
        this.score = score;
    }
}
