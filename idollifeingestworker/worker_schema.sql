-- 取り込みワーカー用の状態テーブル(既読ツイート位置)
-- IdolLife 本体と同じ Supabase プロジェクトの SQL Editor で一度だけ実行してください。
-- ※auto_events テーブルは IdolLife 本体の supabase/schema.sql 側で作成済みです。

create table if not exists public.worker_state (
  account text primary key,
  last_tweet_id text,
  updated_at timestamptz not null default now()
);

-- worker_state と auto_events への書き込みは service_role キー(GitHub Secrets)のみ。
-- anon/authenticated には触らせない(RLSを有効化し、ポリシーは作らない=全拒否)。
alter table public.worker_state enable row level security;
revoke all on public.worker_state from anon, authenticated;
