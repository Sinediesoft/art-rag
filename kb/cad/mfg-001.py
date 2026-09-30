# L 型固定支架 BRK-1001（單位 mm）：底板 80×50×8 ＋ 立板 8×50×60，一體銑削
import cadquery as cq

base = cq.Workplane("XY").box(80, 50, 8, centered=(True, True, False))
wall = cq.Workplane("XY").box(8, 50, 60, centered=(True, True, False)).translate((-36, 0, 0))
# 底板 4-Ø6.6 通孔（M6 螺栓）
base_holes = (
    cq.Workplane("XY").pushPoints([(10, -12), (10, 12), (28, -12), (28, 12)]).circle(3.3).extrude(8)
)
# 立板 2-Ø8.5 通孔（M8 螺栓）
wall_holes = (
    cq.Workplane("YZ")
    .pushPoints([(-12, 42), (12, 42)])
    .circle(4.25)
    .extrude(8)
    .translate((-40, 0, 0))
)
solid = base.union(wall).cut(base_holes).cut(wall_holes)
