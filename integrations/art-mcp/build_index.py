"""
build_index.py：建立索引（學習地圖 A2～A4：切段 → 向量化 → 存進卡櫃）

由 build_index.bat 執行。資料有新增或修改時，都要重新執行一次。
第一次執行會下載向量模型（約 90 MB），之後就不用再下載。
"""

import sys
import time

import search


def main() -> int:
    t0 = time.time()
    print("【A1】讀取 data/artworks/ 裡的作品資料 ...")
    artworks, problems = search.load_artworks()
    for p in problems:
        print(f"   ⚠ 跳過：{p}")
    if not artworks:
        print("   ✘ 沒有可用的作品資料，停止。")
        return 1
    print(f"   ✔ 讀到 {len(artworks)} 件作品")

    print("【A2】把描述切成小段（一段只講一件事，最多約 300 字）...")
    passages, skipped = search.make_passages(artworks)
    for s in skipped:
        print(f"   ⚠ 不收這段：{s}")
    print(f"   ✔ 切成 {len(passages)} 段")

    print("【A3】把每一段轉成 512 個數字（向量化）...")
    if search.FAKE_EMBED:
        print("   （注意：現在用的是雲端測試用的假向量，不是真的模型）")
    else:
        print(f"   模型：{search.MODEL_NAME}（第一次會先下載約 90 MB，請耐心等）")
    embedder = search.Embedder(allow_download=True)
    vectors = embedder.embed([p.text_for_embedding() for p in passages])
    print(f"   ✔ 完成，數字表大小：{vectors.shape[0]} 段 × {vectors.shape[1]} 個數字")

    print("【A4】存進 index/ 資料夾 ...")
    meta = search.save_index(passages, vectors, embedder, n_artworks=len(artworks))
    print(f"   ✔ 已存好（資料指紋 {meta['data_fingerprint']}）")

    print()
    print(f"全部完成，花了 {time.time() - t0:.1f} 秒。")
    print("看看第一段長什麼樣：")
    p = passages[0]
    print(f"   {p.passage_id}｜{p.title}｜{p.topic}")
    print(f"   文字：{p.text[:60]}…")
    print(
        f"   向量前 5 個數字：{[round(float(x), 3) for x in vectors[0][:5]]} …（共 {vectors.shape[1]} 個）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
