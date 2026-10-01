package artrag.scheduler.domain;

import java.util.ArrayList;
import java.util.List;

import ai.timefold.solver.core.api.domain.common.PlanningId;
import ai.timefold.solver.core.api.domain.entity.PlanningEntity;
import ai.timefold.solver.core.api.domain.entity.PlanningPinToIndex;
import ai.timefold.solver.core.api.domain.valuerange.ValueRangeProvider;
import ai.timefold.solver.core.api.domain.variable.PlanningListVariable;

/**
 * 機台（規劃實體）：Timefold 決定每台機台依序做哪些工序（list variable）。
 * <p>
 * 每台機台只能做同機型的工序（{@link #getAllowedOperations()} 是這台機台的可選範圍），
 * 生產中的工序事先放在清單最前面並釘選（{@link #firstUnpinnedIndex}），求解時不會被移動。
 */
@PlanningEntity
public class Machine {

    @PlanningId
    private String id;
    private String type;

    @PlanningListVariable(valueRangeProviderRefs = "allowedOperations")
    private List<Operation> operations = new ArrayList<>();

    @PlanningPinToIndex
    private int firstUnpinnedIndex;

    private List<Operation> allowedOperations = new ArrayList<>();

    public Machine() {
    }

    public Machine(String id, String type) {
        this.id = id;
        this.type = type;
    }

    @ValueRangeProvider(id = "allowedOperations")
    public List<Operation> getAllowedOperations() {
        return allowedOperations;
    }

    public void setAllowedOperations(List<Operation> allowedOperations) {
        this.allowedOperations = allowedOperations;
    }

    public String getId() {
        return id;
    }

    public String getType() {
        return type;
    }

    public List<Operation> getOperations() {
        return operations;
    }

    public void setOperations(List<Operation> operations) {
        this.operations = operations;
    }

    public int getFirstUnpinnedIndex() {
        return firstUnpinnedIndex;
    }

    public void setFirstUnpinnedIndex(int firstUnpinnedIndex) {
        this.firstUnpinnedIndex = firstUnpinnedIndex;
    }

    @Override
    public String toString() {
        return id;
    }
}
