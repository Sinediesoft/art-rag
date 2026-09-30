# 治具定位板 FXP-7007（單位 mm）：150×100×15，2-Ø10H7 定位銷孔，4-M8 沉頭孔，中央避讓槽 60×40×5
import cadquery as cq

plate = cq.Workplane("XY").box(150, 100, 15, centered=(True, True, False))
dowels = cq.Workplane("XY").pushPoints([(-60, 35), (60, -35)]).circle(5).extrude(15)
pocket = cq.Workplane("XY").workplane(offset=10).rect(60, 40).extrude(5)
solid = (
    plate.cut(dowels)
    .cut(pocket)
    .faces(">Z")
    .workplane(origin=(0, 0, 15))
    .pushPoints([(-60, -35), (60, 35), (-25, -38), (25, 38)])
    .cboreHole(9, 14, 8.6)
)
