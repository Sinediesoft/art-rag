package artrag.scheduler.solver;

import ai.timefold.solver.core.api.score.HardMediumSoftScore;
import ai.timefold.solver.core.api.score.stream.Constraint;
import ai.timefold.solver.core.api.score.stream.ConstraintFactory;
import ai.timefold.solver.core.api.score.stream.ConstraintProvider;

import artrag.scheduler.domain.Operation;

/**
 * 排程的限制條件（Constraint Streams）。Timefold 的限制條件 ID 只能用英數字，
 * 中文名稱與各條件的分數明細由後端 app/scheduling/solution.py 的 evaluate() 用同樣的規則計算
 * （Timefold 2.x 的 Score analysis 屬商業版），並核對總分與 Timefold 一致；Timefold 不可用時也用它計分。
 * <ul>
 * <li>硬：機型不符（機台的可選範圍已限制，這條是保險）</li>
 * <li>中：交期延遲＝延遲的工作分鐘 × 工單權重（急件權重較高）</li>
 * <li>軟：換線準備＝實際花的準備分鐘；完工時間＝各工單完工的工作分鐘（越早越好）</li>
 * </ul>
 * 工序之間互相等待（循環）由 Timefold 的 structural score 處理，forEach 會自動濾掉這些不一致的工序。
 */
public class ProductionConstraints implements ConstraintProvider {

    public static final String MACHINE_TYPE = "Machine type mismatch"; // 機型不符
    public static final String TARDINESS = "Tardiness"; // 交期延遲
    public static final String SETUP = "Setup time"; // 換線準備
    public static final String COMPLETION = "Completion time"; // 完工時間

    @Override
    public Constraint[] defineConstraints(ConstraintFactory factory) {
        return new Constraint[] {
                machineType(factory),
                tardiness(factory),
                setupTime(factory),
                completionTime(factory)
        };
    }

    public Constraint machineType(ConstraintFactory factory) {
        return factory.forEach(Operation.class)
                .filter(op -> !op.getMachine().getType().equals(op.getMachineType()))
                .penalize(HardMediumSoftScore.ONE_HARD)
                .asConstraint(MACHINE_TYPE);
    }

    public Constraint tardiness(ConstraintFactory factory) {
        return factory.forEach(Operation.class)
                .filter(op -> op.isLastInJob() && op.getEndMin() != null && op.getLateMin() > 0)
                .penalize(HardMediumSoftScore.ONE_MEDIUM, op -> op.getLateMin() * op.getWorkOrder().weight())
                .asConstraint(TARDINESS);
    }

    public Constraint setupTime(ConstraintFactory factory) {
        return factory.forEach(Operation.class)
                .filter(op -> op.getEndMin() != null && op.getSetupApplied() > 0)
                .penalize(HardMediumSoftScore.ONE_SOFT, Operation::getSetupApplied)
                .asConstraint(SETUP);
    }

    public Constraint completionTime(ConstraintFactory factory) {
        return factory.forEach(Operation.class)
                .filter(op -> op.isLastInJob() && op.getEndMin() != null)
                .penalize(HardMediumSoftScore.ONE_SOFT, Operation::getCompletionMin)
                .asConstraint(COMPLETION);
    }
}
