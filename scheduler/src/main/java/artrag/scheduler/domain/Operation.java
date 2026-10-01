package artrag.scheduler.domain;

import ai.timefold.solver.core.api.domain.common.PlanningId;
import ai.timefold.solver.core.api.domain.entity.PlanningEntity;
import ai.timefold.solver.core.api.domain.variable.InverseRelationShadowVariable;
import ai.timefold.solver.core.api.domain.variable.PreviousElementShadowVariable;
import ai.timefold.solver.core.api.domain.variable.ShadowSources;
import ai.timefold.solver.core.api.domain.variable.ShadowVariable;

/**
 * 工序（list variable 的元素）：一張工單在途程上的一道自製工序。
 * <p>
 * 排在哪台機台、前一道是誰由 Timefold 決定；起訖時間是宣告式影子變數（declarative shadow variable），
 * 由下列規則推算，Timefold 會在每次移動後只重算受影響的工序：
 * <pre>
 *   開始 = max(工單可開工, 同機台前一道完成, 同工單前一道完成 + 委外天數)
 *   完成 = 開始 + 換線準備（同機台前一道是同圖紙同工序就免） + 加工時間
 * </pre>
 * 若同機台順序與工序先後互相矛盾（A 等 B、B 又等 A），Timefold 會把這組解標為結構不良（structural score），
 * 不需要另寫限制條件。
 */
@PlanningEntity
public class Operation {

    @PlanningId
    private String id;
    private WorkOrder workOrder;
    private int seq;
    private String machineType;
    private long setupMin;
    private long runMin;
    /** 這道工序完成後的委外等待（熱處理、表面處理），工作分鐘 */
    private long lagAfterMin;
    /** 換線家族：同圖紙同一道工序，連續做可省準備時間 */
    private String setupFamily;
    /** 生產中的工序：已在機台上、不必換線，釘選在清單最前面 */
    private boolean pinned;
    /** 同工單的前一道自製工序（problem fact） */
    private Operation previousInJob;
    private boolean lastInJob;

    @InverseRelationShadowVariable(sourceVariableName = "operations")
    private Machine machine;

    @PreviousElementShadowVariable(sourceVariableName = "operations")
    private Operation previousOnMachine;

    @ShadowVariable(supplierName = "startSupplier")
    private Long startMin;

    @ShadowVariable(supplierName = "endSupplier")
    private Long endMin;

    public Operation() {
    }

    public Operation(String id, WorkOrder workOrder, int seq, String machineType, long setupMin, long runMin,
            long lagAfterMin, String setupFamily, boolean pinned) {
        this.id = id;
        this.workOrder = workOrder;
        this.seq = seq;
        this.machineType = machineType;
        this.setupMin = setupMin;
        this.runMin = runMin;
        this.lagAfterMin = lagAfterMin;
        this.setupFamily = setupFamily;
        this.pinned = pinned;
    }

    // ************************************************************************
    // 影子變數的推算
    // ************************************************************************

    @ShadowSources({ "machine", "previousOnMachine.endMin", "previousInJob.endMin", "previousInJob.machine" })
    public Long startSupplier() {
        if (machine == null) {
            return null;
        }
        long ready = workOrder.releaseMin();
        if (previousOnMachine != null) {
            if (previousOnMachine.getEndMin() == null) {
                return null;
            }
            ready = Math.max(ready, previousOnMachine.getEndMin());
        }
        // 前一道工序還沒排（建構解的過程中）就先不管它，排上之後會自動重算
        if (previousInJob != null && previousInJob.getMachine() != null) {
            if (previousInJob.getEndMin() == null) {
                return null;
            }
            ready = Math.max(ready, previousInJob.getEndMin() + previousInJob.getLagAfterMin());
        }
        return ready;
    }

    @ShadowSources({ "startMin", "previousOnMachine" })
    public Long endSupplier() {
        return startMin == null ? null : startMin + getSetupApplied() + runMin;
    }

    /** 實際的換線準備時間：生產中的工序、或同機台前一道是同一換線家族就免 */
    public long getSetupApplied() {
        if (pinned) {
            return 0;
        }
        if (previousOnMachine != null && setupFamily.equals(previousOnMachine.getSetupFamily())) {
            return 0;
        }
        return setupMin;
    }

    /** 工單完工時間（含最後的委外天數）；只對最後一道工序有意義 */
    public Long getCompletionMin() {
        return endMin == null ? null : endMin + lagAfterMin;
    }

    public long getLateMin() {
        Long completion = getCompletionMin();
        return completion == null ? 0 : Math.max(0, completion - workOrder.dueMin());
    }

    // ************************************************************************
    // Getters and setters
    // ************************************************************************

    public String getId() {
        return id;
    }

    public WorkOrder getWorkOrder() {
        return workOrder;
    }

    public int getSeq() {
        return seq;
    }

    public String getMachineType() {
        return machineType;
    }

    public long getSetupMin() {
        return setupMin;
    }

    public long getRunMin() {
        return runMin;
    }

    public long getLagAfterMin() {
        return lagAfterMin;
    }

    public String getSetupFamily() {
        return setupFamily;
    }

    public boolean isPinned() {
        return pinned;
    }

    public Operation getPreviousInJob() {
        return previousInJob;
    }

    public void setPreviousInJob(Operation previousInJob) {
        this.previousInJob = previousInJob;
    }

    public boolean isLastInJob() {
        return lastInJob;
    }

    public void setLastInJob(boolean lastInJob) {
        this.lastInJob = lastInJob;
    }

    public Machine getMachine() {
        return machine;
    }

    public void setMachine(Machine machine) {
        this.machine = machine;
    }

    public Operation getPreviousOnMachine() {
        return previousOnMachine;
    }

    public void setPreviousOnMachine(Operation previousOnMachine) {
        this.previousOnMachine = previousOnMachine;
    }

    public Long getStartMin() {
        return startMin;
    }

    public void setStartMin(Long startMin) {
        this.startMin = startMin;
    }

    public Long getEndMin() {
        return endMin;
    }

    public void setEndMin(Long endMin) {
        this.endMin = endMin;
    }

    @Override
    public String toString() {
        return id;
    }
}
