"""說了「知識庫中沒有這方面的資料。」後面卻又引用資料回答：拿掉那句（實際評估回答裡看到的樣子）。"""

from app.rag.textproc import NO_DATA, drop_contradicting_no_data


def test_leading_no_data_before_a_cited_correction_is_removed():
    answer = (
        "知識庫中沒有這方面的資料。   董其昌是明代書畫家，他在畫幅上方題字；"
        "是故宮研究人員李霖燦在 1958 年發現的 [1]。"
    )
    out = drop_contradicting_no_data(answer)
    assert NO_DATA[:-1] not in out and out.startswith("董其昌") and out.endswith("[1]。")
    answer = "知識庫中沒有這方面的資料。畫中女子牽的動物是猴子 [1][畫面]，並未顯示為狗。"
    assert drop_contradicting_no_data(answer) == "畫中女子牽的動物是猴子 [1][畫面]，並未顯示為狗。"


def test_no_data_in_the_middle_with_cited_text_after_is_removed():
    answer = (
        "〈大碗島〉完成於 1884–1886 年 [1]。\n知識庫中沒有這方面的資料。\n"
        "秀拉的畫作早於梵谷的畫作完成 [1][2]。"
    )
    out = drop_contradicting_no_data(answer)
    assert NO_DATA[:-1] not in out and "[1][2]" in out


def test_kept_when_nothing_cited_follows_or_when_it_is_the_only_answer():
    # 真的答不出來
    assert drop_contradicting_no_data(NO_DATA) == NO_DATA
    # 最後一句：可能是在說其餘的部分查不到，交給保守的 drop_stray_no_data
    tail = "這幅畫是梵谷畫的 [1]。知識庫中沒有這方面的資料。"
    assert drop_contradicting_no_data(tail) == tail
    # 接在句中、不是一整句
    inline = "包裝方式：知識庫中沒有這方面的資料。材料為 FC250 [1]。"
    assert drop_contradicting_no_data(inline) == inline
