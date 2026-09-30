# 立式軸承座 PBK-6006（單位 mm）：底座 120×40×16，軸承孔 Ø35H7 中心高 40，2-Ø11 安裝孔，Ø6 注油孔
import cadquery as cq

base = cq.Workplane("XY").box(120, 40, 16, centered=(True, True, False))
body = cq.Workplane("XY").box(64, 40, 40, centered=(True, True, False))
cap = cq.Workplane("XZ").center(0, 40).circle(32).extrude(20, both=True)
bore = cq.Workplane("XZ").center(0, 40).circle(17.5).extrude(20, both=True)
mount = cq.Workplane("XY").pushPoints([(-46, 0), (46, 0)]).circle(5.5).extrude(16)
grease = cq.Workplane("XY").workplane(offset=50).circle(3).extrude(30)
solid = base.union(body).union(cap).cut(bore).cut(mount).cut(grease)
