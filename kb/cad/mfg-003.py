# 階梯傳動軸 SFT-3003（單位 mm）：Ø20×30／Ø30×60／Ø24×20／Ø20×35，Ø30 段鍵槽 8×4×40
import cadquery as cq


def section(d, x0, length):
    return cq.Workplane("YZ").workplane(offset=x0).circle(d / 2).extrude(length)


shaft = (
    section(20, 0, 30)
    .union(section(30, 30, 60))
    .union(section(24, 90, 20))
    .union(section(20, 110, 35))
)
keyway = cq.Workplane("XY").box(40, 8, 4, centered=(False, True, False)).translate((40, 0, 11))
solid = shaft.cut(keyway)
