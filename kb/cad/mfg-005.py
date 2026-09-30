# T 型槽螺帽 TSN-5005（單位 mm）：配 14 mm T 型槽
# 凸緣 24×40×8＋頸部 14×40×9，中心 M10 螺紋底孔 Ø8.5
import cadquery as cq

flange = cq.Workplane("XY").box(24, 40, 8, centered=(True, True, False))
neck = cq.Workplane("XY").box(14, 40, 9, centered=(True, True, False)).translate((0, 0, 8))
tap = cq.Workplane("XY").circle(4.25).extrude(17)
solid = flange.union(neck).cut(tap)
