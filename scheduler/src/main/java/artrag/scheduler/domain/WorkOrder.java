package artrag.scheduler.domain;

/**
 * 工單（problem fact）：可開工時間、交期與延遲權重（急件權重較高）。時間單位都是工作分鐘。
 */
public record WorkOrder(String id, long releaseMin, long dueMin, int weight) {
}
