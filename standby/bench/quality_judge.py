#!/usr/bin/env python3
"""検索の質（2）: quality_collect.py の出力を、TypeSafe（Jev）で「この問いに関係ある記憶か」を判定して集計する。
ホストの python3 で動かす（リポジトリの scripts/judge.py を使う）。質問は scripts/hindsight-recall-hook.py の JEV_QUESTION と同じ。
候補の本文はフックと同じく 300 字まで。履歴（history）は付けない（この場では取れない）。

  python3 bench/quality_judge.py IN.json OUT.json [--nilkani06-dir ~/nilkani06]
"""
import argparse, json, os, sys

Q = ("`message` is a message a person just sent to a chat bot, `history` (if present) is the conversation "
     "just before it in the same channel, and `memories` holds things the bot remembers. Memory `memories.{key}` "
     "is relevant to `message`: it is about the same topic, person, project or earlier decision, so recalling it "
     "would help the bot understand or answer `message`, as opposed to only sharing a few words with it or being "
     "about something else.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp"); ap.add_argument("out")
    ap.add_argument("--nilkani06-dir", default=os.path.expanduser("~/nilkani06"))
    ap.add_argument("--threshold", type=float, default=0.5)
    a = ap.parse_args()
    sys.path.insert(0, os.path.join(a.nilkani06_dir, "scripts"))
    import judge
    d = json.load(open(a.inp))
    res = []
    for q in d["queries"]:
        ids = list(dict.fromkeys([x["id"] for x in q["bge_top"]] + [x["id"] for x in q["sar_top"]]))
        keys = {i: f"m{n:02d}" for n, i in enumerate(ids)}
        state = {"message": q["text"], "memories": {keys[i]: " ".join(d["cands"][i]["text"].split())[:300] for i in ids}}
        qs = {keys[i]: {"type": "yesno", "question": Q.format(key=keys[i])} for i in ids}
        ans = judge.ask(state, qs, timeout=60)
        if not isinstance(ans, dict):
            print("Jev が使えなかった／失敗:", q["message_id"]); res.append({"message_id": q["message_id"], "p": None}); continue
        p = {i: float(ans[keys[i]]["p"]) for i in ids}
        res.append({"message_id": q["message_id"], "p": p})
        print(q["message_id"], "judged", len(ids), flush=True)
    json.dump(res, open(a.out, "w"))
    # 集計
    import statistics as st
    rows = []
    for q, r in zip(d["queries"], res):
        if r["p"] is None: continue
        def stat(top):
            ps = [r["p"][x["id"]] for x in top]
            return sum(p >= a.threshold for p in ps), st.mean(ps), sum(p >= a.threshold for p in ps[:5])
        rows.append((stat(q["bge_top"]), stat(q["sar_top"]), q["overlap"]))
    n = len(rows)
    print(f"判定できた問い {n}/{len(d['queries'])}")
    if n:
        for name, idx in (("bge-m3", 0), ("Sarashina", 1)):
            print(f"{name:10s} 上位10の関連数(p>={a.threshold}) 平均 {st.mean(r[idx][0] for r in rows):.2f}  "
                  f"上位5の関連数 平均 {st.mean(r[idx][2] for r in rows):.2f}  p 平均 {st.mean(r[idx][1] for r in rows):.3f}")
        w = sum(r[1][0] > r[0][0] for r in rows); l = sum(r[1][0] < r[0][0] for r in rows)
        print(f"問いごとの関連数: Sarashina が多い {w}、bge-m3 が多い {l}、同じ {n-w-l}")


if __name__ == "__main__":
    main()
