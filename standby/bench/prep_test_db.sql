-- 試験用 DB（本番の複製）で、試験用 Hindsight が勝手に動かないようにする。hindsight_standby_test にだけ流す。
-- 本番から複製した時点の pending / processing の非同期タスク（consolidation など）を cancelled にする。
UPDATE async_operations SET status = 'cancelled' WHERE status IN ('pending', 'processing');
