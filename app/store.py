"""SQLite 存储。只用标准库，单文件数据库，重启后历史任务与报告仍在。"""
import gzip
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from .config import DB_PATH

DEFAULT_RATE_MULTIPLIERS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0)

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  protocol TEXT NOT NULL DEFAULT 'openai',
  base_url TEXT NOT NULL,
  group_name TEXT DEFAULT '',
  env TEXT DEFAULT 'prod',
  key_enc TEXT NOT NULL,
  source TEXT DEFAULT 'manual',
  edited_fields TEXT DEFAULT '[]',
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id INTEGER,
  name TEXT NOT NULL,
  protocol TEXT NOT NULL DEFAULT 'openai',
  base_url TEXT NOT NULL,
  model TEXT NOT NULL,
  group_name TEXT DEFAULT '',
  env TEXT DEFAULT 'prod',
  key_enc TEXT DEFAULT '',
  price_in REAL, price_out REAL,
  upstream_multiplier REAL,
  source TEXT DEFAULT 'manual',        -- import / manual
  edited_fields TEXT DEFAULT '[]',     -- 导入后被手工改过的字段
  recorded INTEGER DEFAULT 0,          -- 接入检测通过才置 1
  status TEXT DEFAULT 'pending',
  baseline TEXT DEFAULT '',            -- 首次通过时的基线快照 JSON
  last_task_id INTEGER, last_verdict TEXT DEFAULT '',
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                  -- admission / inspect / degrade
  target_id INTEGER, target_name TEXT DEFAULT '',
  pack_name TEXT DEFAULT '', pack_version TEXT DEFAULT '',
  status TEXT DEFAULT 'queued',        -- queued/running/success/partial/failed/cancelled/interrupted
  snapshot TEXT DEFAULT '{}',          -- 脱敏后的执行时配置快照
  progress TEXT DEFAULT '{}',
  report TEXT DEFAULT '',
  selected_benchmark_id INTEGER,
  selected_benchmark_comparison TEXT DEFAULT '',
  cost_limit REAL, cancel_flag INTEGER DEFAULT 0,
  parent_task_id INTEGER,              -- 重试来源，原失败记录不被覆盖
  tags_json TEXT DEFAULT '[]', owner TEXT DEFAULT '',
  review_status TEXT DEFAULT 'unreviewed', operator_note TEXT DEFAULT '',
  created_at REAL, started_at REAL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL, ts REAL,
  level TEXT DEFAULT 'info', stage TEXT DEFAULT '', message TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_kind ON tasks(kind, created_at);
CREATE TABLE IF NOT EXISTS job_leases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL UNIQUE,
  owner_id TEXT NOT NULL,
  lease_until REAL NOT NULL,
  heartbeat_at REAL NOT NULL,
  acquired_at REAL NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_job_leases_expiry ON job_leases(lease_until);
CREATE TABLE IF NOT EXISTS system_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dedupe_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'warning',
  status TEXT NOT NULL DEFAULT 'open',
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  occurrence_count INTEGER NOT NULL DEFAULT 1,
  first_seen_at REAL NOT NULL,
  last_seen_at REAL NOT NULL,
  resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_system_alerts_status ON system_alerts(status,last_seen_at);
CREATE TABLE IF NOT EXISTS backup_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_date TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  backup_path TEXT NOT NULL DEFAULT '',
  manifest_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL,
  finished_at REAL,
  UNIQUE(run_date,kind)
);
CREATE TABLE IF NOT EXISTS task_report_archives (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL UNIQUE,
  report_gzip BLOB NOT NULL,
  original_bytes INTEGER NOT NULL,
  compressed_bytes INTEGER NOT NULL,
  archived_at REAL NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS benchmarks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  pack_version TEXT NOT NULL,          -- 题库版本，跨版本拒绝对比
  model_hint TEXT DEFAULT '',          -- 这份标杆是用哪个模型定的
  dims TEXT DEFAULT '{}',              -- {维度: 分数}
  items TEXT DEFAULT '{}',             -- {题目 id: 分数}，留证据
  overall REAL,
  tolerance REAL DEFAULT 0.10,         -- 允许低于标杆多少才算不达标
  source TEXT DEFAULT 'task',          -- task（跑出来的）/ manual（手填的）
  source_task_id INTEGER,
  note TEXT DEFAULT '',
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,                  -- 0.5x组 / 1x组 / 2x组
  multiplier REAL NOT NULL,            -- 倍率，推荐时按这个排序
  benchmark_id INTEGER,                -- 该组的黄金标杆（步骤 3）
  note TEXT DEFAULT '',
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS model_families (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS model_family_models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  family_id INTEGER NOT NULL,
  model TEXT NOT NULL,
  display_name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at REAL,
  updated_at REAL,
  UNIQUE(family_id,model),
  FOREIGN KEY(family_id) REFERENCES model_families(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS platform_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  family_id INTEGER NOT NULL,
  multiplier REAL NOT NULL,
  created_at REAL,
  updated_at REAL,
  UNIQUE(family_id,multiplier),
  FOREIGN KEY(family_id) REFERENCES model_families(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS platform_group_benchmarks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform_group_id INTEGER NOT NULL,
  model TEXT NOT NULL,
  benchmark_id INTEGER,
  created_at REAL,
  updated_at REAL,
  UNIQUE(platform_group_id,model),
  FOREIGN KEY(platform_group_id) REFERENCES platform_groups(id) ON DELETE CASCADE,
  FOREIGN KEY(benchmark_id) REFERENCES benchmarks(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deleted_groups (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  model TEXT NOT NULL,
  multiplier REAL NOT NULL,
  benchmark_id INTEGER,
  note TEXT DEFAULT '',
  member_ids TEXT DEFAULT '[]',
  deleted_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  multiplier REAL NOT NULL UNIQUE,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS recommendations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
  status TEXT DEFAULT 'pending',       -- pending/accepted/rejected
  result TEXT DEFAULT '{}',            -- 推荐结论与逐组对比明细
  suggested_group_id INTEGER,
  decided_group_id INTEGER,            -- 人工最终选的组（步骤 6）
  decided_note TEXT DEFAULT '',
  created_at REAL, decided_at REAL
);
CREATE INDEX IF NOT EXISTS idx_recos_target ON recommendations(target_id, created_at);
CREATE TABLE IF NOT EXISTS scheduled_tests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  report_minute INTEGER NOT NULL,
  feishu_webhook_ids TEXT NOT NULL DEFAULT '[]',
  email_recipient_ids TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS scheduled_test_targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_id INTEGER NOT NULL UNIQUE,
  created_at REAL,
  FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS scheduled_report_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_family TEXT NOT NULL,
  multiplier REAL NOT NULL,
  created_at REAL, updated_at REAL,
  UNIQUE(model_family,multiplier)
);
CREATE TABLE IF NOT EXISTS scheduled_report_group_targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL,
  target_id INTEGER NOT NULL,
  created_at REAL,
  UNIQUE(group_id,target_id),
  FOREIGN KEY(group_id) REFERENCES scheduled_report_groups(id) ON DELETE CASCADE,
  FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS scheduled_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scheduled_test_id INTEGER NOT NULL,
  run_date TEXT NOT NULL,
  report_at REAL NOT NULL,
  task_ids TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'testing',
  initial_sent INTEGER NOT NULL DEFAULT 0,
  initial_complete INTEGER NOT NULL DEFAULT 0,
  supplement_sent INTEGER NOT NULL DEFAULT 0,
  initial_summary TEXT DEFAULT '',
  final_summary TEXT DEFAULT '',
  created_at REAL, updated_at REAL,
  UNIQUE(scheduled_test_id, run_date)
);
CREATE TABLE IF NOT EXISTS feishu_webhooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  webhook_enc TEXT NOT NULL,
  secret_enc TEXT DEFAULT '',
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS smtp_settings (
  id INTEGER PRIMARY KEY CHECK (id=1),
  host TEXT NOT NULL,
  port INTEGER NOT NULL,
  tls_mode TEXT NOT NULL,
  username TEXT NOT NULL,
  password_enc TEXT NOT NULL,
  from_name TEXT DEFAULT '',
  from_address TEXT NOT NULL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS email_recipients (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  address TEXT NOT NULL,
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS notification_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scheduled_run_id INTEGER,
  phase TEXT NOT NULL,
  channel_type TEXT NOT NULL,
  destination_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT DEFAULT '',
  sent_at REAL,
  UNIQUE(scheduled_run_id, phase, channel_type, destination_id)
);
CREATE TABLE IF NOT EXISTS scheduled_score_samples (
  task_id INTEGER PRIMARY KEY,
  target_id INTEGER NOT NULL,
  score_date TEXT NOT NULL,
  score REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled_metric_samples (
  task_id INTEGER PRIMARY KEY,
  scheduled_run_id INTEGER NOT NULL,
  target_id INTEGER NOT NULL,
  finished_at REAL NOT NULL,
  status TEXT NOT NULL,
  pass_rate REAL,
  p95_latency REAL,
  speed REAL,
  timeout_rate REAL,
  stream_break_rate REAL,
  score REAL
);
CREATE TABLE IF NOT EXISTS inspect_trends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_id INTEGER NOT NULL UNIQUE,
  baseline_score REAL NOT NULL,
  warning_active INTEGER NOT NULL DEFAULT 0,
  updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_runs_due ON scheduled_runs(status, report_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_scores_target ON scheduled_score_samples(target_id, score_date);
CREATE INDEX IF NOT EXISTS idx_scheduled_metrics_target ON scheduled_metric_samples(target_id,finished_at);
CREATE TABLE IF NOT EXISTS scheduled_primary_models (
  canonical_model TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled_configuration_targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scheduled_test_id INTEGER NOT NULL,
  configuration_id INTEGER NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(scheduled_test_id,configuration_id),
  FOREIGN KEY(scheduled_test_id) REFERENCES scheduled_tests(id) ON DELETE CASCADE,
  FOREIGN KEY(configuration_id) REFERENCES channel_configurations(id)
);
CREATE INDEX IF NOT EXISTS idx_scheduled_configuration_targets_schedule
  ON scheduled_configuration_targets(scheduled_test_id,configuration_id);
CREATE TABLE IF NOT EXISTS scheduled_plan_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scheduled_test_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  configuration_fingerprints_json TEXT NOT NULL,
  primary_models_json TEXT NOT NULL,
  measurement_rules_json TEXT NOT NULL,
  threshold_version TEXT NOT NULL,
  baseline_status TEXT NOT NULL DEFAULT 'building',
  baseline_json TEXT NOT NULL DEFAULT '{}',
  created_by INTEGER,
  reason TEXT NOT NULL DEFAULT '',
  active_at REAL NOT NULL,
  superseded_at REAL,
  created_at REAL NOT NULL,
  UNIQUE(scheduled_test_id,version),
  FOREIGN KEY(scheduled_test_id) REFERENCES scheduled_tests(id) ON DELETE CASCADE,
  FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_plan_versions_active
  ON scheduled_plan_versions(scheduled_test_id,superseded_at);
CREATE TABLE IF NOT EXISTS scheduled_measurement_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  original_request_id INTEGER,
  configuration_id INTEGER NOT NULL,
  canonical_model TEXT NOT NULL,
  template_id TEXT NOT NULL,
  template_kind TEXT NOT NULL,
  request_index INTEGER NOT NULL,
  attempt_kind TEXT NOT NULL,
  sent_at REAL,
  finished_at REAL,
  monotonic_started REAL,
  monotonic_finished REAL,
  status TEXT NOT NULL,
  attribution TEXT NOT NULL DEFAULT '',
  error_code TEXT NOT NULL DEFAULT '',
  error_detail TEXT NOT NULL DEFAULT '',
  response_summary TEXT NOT NULL DEFAULT '',
  metrics_json TEXT NOT NULL DEFAULT '{}',
  evidence_hash TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
  FOREIGN KEY(original_request_id) REFERENCES scheduled_measurement_requests(id),
  FOREIGN KEY(configuration_id) REFERENCES channel_configurations(id)
);
CREATE INDEX IF NOT EXISTS idx_scheduled_measurements_task
  ON scheduled_measurement_requests(task_id,request_index,id);
CREATE TABLE IF NOT EXISTS scheduled_measurement_raw_blocks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  block_type TEXT NOT NULL,
  key_ciphertext TEXT NOT NULL,
  ciphertext TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  created_at REAL NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS scheduled_measurement_evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  request_id INTEGER,
  record_seq INTEGER NOT NULL,
  record_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  raw_block_id INTEGER,
  previous_hash TEXT NOT NULL,
  record_hash TEXT NOT NULL,
  record_hmac TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(task_id,record_seq),
  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
  FOREIGN KEY(request_id) REFERENCES scheduled_measurement_requests(id) ON DELETE SET NULL
  ,FOREIGN KEY(raw_block_id) REFERENCES scheduled_measurement_raw_blocks(id)
);
CREATE TABLE IF NOT EXISTS scheduled_measurement_report_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  reason TEXT NOT NULL,
  report_json TEXT NOT NULL,
  input_evidence_root TEXT NOT NULL,
  created_by INTEGER,
  created_at REAL NOT NULL,
  UNIQUE(task_id,version),
  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
  FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS scheduled_attribution_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER NOT NULL,
  attribution TEXT NOT NULL,
  reason TEXT NOT NULL,
  user_id INTEGER,
  actor TEXT NOT NULL,
  created_at REAL NOT NULL,
  FOREIGN KEY(request_id) REFERENCES scheduled_measurement_requests(id) ON DELETE CASCADE,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS scheduled_anomaly_states (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scheduled_test_id INTEGER NOT NULL,
  plan_version_id INTEGER NOT NULL,
  configuration_id INTEGER NOT NULL,
  canonical_model TEXT NOT NULL,
  category TEXT NOT NULL,
  state TEXT NOT NULL,
  consecutive_hits INTEGER NOT NULL DEFAULT 0,
  last_task_id INTEGER,
  last_report_version INTEGER,
  last_changed_at REAL NOT NULL,
  last_notified_at REAL,
  details_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(plan_version_id,configuration_id,canonical_model,category),
  FOREIGN KEY(scheduled_test_id) REFERENCES scheduled_tests(id) ON DELETE CASCADE,
  FOREIGN KEY(plan_version_id) REFERENCES scheduled_plan_versions(id) ON DELETE CASCADE,
  FOREIGN KEY(configuration_id) REFERENCES channel_configurations(id) ON DELETE CASCADE,
  FOREIGN KEY(last_task_id) REFERENCES tasks(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS scheduled_anomaly_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  anomaly_state_id INTEGER NOT NULL,
  task_id INTEGER,
  report_version INTEGER,
  previous_state TEXT NOT NULL,
  next_state TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  reason TEXT NOT NULL,
  created_at REAL NOT NULL,
  FOREIGN KEY(anomaly_state_id) REFERENCES scheduled_anomaly_states(id) ON DELETE CASCADE,
  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_anomaly_current
  ON scheduled_anomaly_states(scheduled_test_id,plan_version_id,state);
CREATE TABLE IF NOT EXISTS usage_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  critical INTEGER NOT NULL DEFAULT 0,
  description TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_profile_bindings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_type TEXT NOT NULL,
  subject_hash TEXT NOT NULL,
  subject_label TEXT NOT NULL DEFAULT '',
  primary_profile TEXT NOT NULL DEFAULT 'general',
  secondary_profiles TEXT NOT NULL DEFAULT '[]',
  note TEXT NOT NULL DEFAULT '',
  created_by INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(subject_type,subject_hash),
  FOREIGN KEY(primary_profile) REFERENCES usage_profiles(code),
  FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_bindings_profile
  ON usage_profile_bindings(primary_profile,subject_type);
CREATE TABLE IF NOT EXISTS metric_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  endpoint TEXT NOT NULL,
  token_enc TEXT NOT NULL DEFAULT '',
  protocol_version TEXT NOT NULL DEFAULT '1',
  cursor TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  poll_interval_seconds INTEGER NOT NULL DEFAULT 60,
  last_attempt_at REAL,
  last_success_at REAL,
  last_error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_buckets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL,
  bucket_start INTEGER NOT NULL,
  platform_group TEXT NOT NULL,
  model_family TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL,
  usage_profile TEXT NOT NULL DEFAULT 'general',
  channel TEXT NOT NULL,
  supply_source TEXT NOT NULL DEFAULT '',
  output_length_band TEXT NOT NULL DEFAULT 'unknown',
  request_count INTEGER NOT NULL DEFAULT 0,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0,
  failure_count INTEGER NOT NULL DEFAULT 0,
  retry_count INTEGER NOT NULL DEFAULT 0,
  failover_count INTEGER NOT NULL DEFAULT 0,
  auth_error_count INTEGER NOT NULL DEFAULT 0,
  rate_limit_count INTEGER NOT NULL DEFAULT 0,
  timeout_count INTEGER NOT NULL DEFAULT 0,
  stream_break_count INTEGER NOT NULL DEFAULT 0,
  upstream_5xx_count INTEGER NOT NULL DEFAULT 0,
  network_error_count INTEGER NOT NULL DEFAULT 0,
  protocol_error_count INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  ttft_p50_ms REAL,
  ttft_p95_ms REAL,
  latency_p50_ms REAL,
  latency_p95_ms REAL,
  generation_tps REAL,
  output_length_p50 REAL,
  output_length_p95 REAL,
  active_users INTEGER NOT NULL DEFAULT 0,
  top_user_request_share REAL NOT NULL DEFAULT 0,
  received_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(source_id,bucket_start,platform_group,model,usage_profile,channel,output_length_band),
  FOREIGN KEY(source_id) REFERENCES metric_sources(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_metric_buckets_time ON metric_buckets(bucket_start);
CREATE INDEX IF NOT EXISTS idx_metric_buckets_dimensions
  ON metric_buckets(platform_group,model,usage_profile,channel,bucket_start);
CREATE TABLE IF NOT EXISTS metric_collection_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL,
  cursor_before TEXT NOT NULL DEFAULT '',
  cursor_after TEXT NOT NULL DEFAULT '',
  received_count INTEGER NOT NULL DEFAULT 0,
  upserted_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL,
  finished_at REAL,
  FOREIGN KEY(source_id) REFERENCES metric_sources(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_metric_runs_source
  ON metric_collection_runs(source_id,started_at);
CREATE TABLE IF NOT EXISTS hourly_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_start INTEGER NOT NULL,
  platform_group TEXT NOT NULL,
  model_family TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL,
  usage_profile TEXT NOT NULL,
  channel TEXT NOT NULL,
  output_length_band TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(period_start,platform_group,model,usage_profile,channel,output_length_band)
);
CREATE INDEX IF NOT EXISTS idx_hourly_metrics_time ON hourly_metrics(period_start);
CREATE TABLE IF NOT EXISTS daily_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_start INTEGER NOT NULL,
  platform_group TEXT NOT NULL,
  model_family TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL,
  usage_profile TEXT NOT NULL,
  channel TEXT NOT NULL,
  output_length_band TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(period_start,platform_group,model,usage_profile,channel,output_length_band)
);
CREATE INDEX IF NOT EXISTS idx_daily_metrics_time ON daily_metrics(period_start);
CREATE TABLE IF NOT EXISTS metric_retention_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  hourly_rows INTEGER NOT NULL DEFAULT 0,
  daily_rows INTEGER NOT NULL DEFAULT 0,
  minute_rows_deleted INTEGER NOT NULL DEFAULT 0,
  hourly_rows_deleted INTEGER NOT NULL DEFAULT 0,
  daily_rows_deleted INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL,
  finished_at REAL
);
CREATE TABLE IF NOT EXISTS supply_gap_recommendations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  week_start INTEGER NOT NULL,
  platform_group TEXT NOT NULL,
  model TEXT NOT NULL,
  usage_profile TEXT NOT NULL,
  demand_level TEXT NOT NULL,
  request_count INTEGER NOT NULL,
  active_users INTEGER NOT NULL,
  required_channels INTEGER NOT NULL,
  qualified_channels INTEGER NOT NULL,
  concentration REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'candidate',
  reasons_json TEXT NOT NULL DEFAULT '[]',
  suspected_fault_domains_json TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(week_start,platform_group,model,usage_profile)
);
CREATE INDEX IF NOT EXISTS idx_supply_gaps_week ON supply_gap_recommendations(week_start,status);
CREATE TABLE IF NOT EXISTS recommendation_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  recommendation_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  production_channel TEXT NOT NULL,
  activated_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'scheduled',
  baseline_json TEXT NOT NULL DEFAULT '{}',
  review_7d_json TEXT NOT NULL DEFAULT '{}',
  review_30d_json TEXT NOT NULL DEFAULT '{}',
  next_review_at REAL NOT NULL,
  created_by INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(recommendation_id,channel_id),
  FOREIGN KEY(recommendation_id) REFERENCES supply_gap_recommendations(id),
  FOREIGN KEY(channel_id) REFERENCES channels(id),
  FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_recommendation_reviews_due
  ON recommendation_reviews(status,next_review_at);
CREATE TABLE IF NOT EXISTS monitor_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  secret_enc TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  last_alert_at REAL,
  last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL,
  channel TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  platform_group TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'collecting',
  first_seen_at REAL NOT NULL,
  last_seen_at REAL NOT NULL,
  alert_count INTEGER NOT NULL DEFAULT 1,
  affected_users INTEGER NOT NULL DEFAULT 0,
  attribution_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  FOREIGN KEY(source_id) REFERENCES monitor_sources(id)
);
CREATE INDEX IF NOT EXISTS idx_incidents_time ON incidents(last_seen_at,status);
CREATE TABLE IF NOT EXISTS incident_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER NOT NULL,
  source_id INTEGER NOT NULL,
  event_id TEXT NOT NULL,
  event_time REAL NOT NULL,
  symptom TEXT NOT NULL,
  severity TEXT NOT NULL,
  affected_users INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  received_at REAL NOT NULL,
  UNIQUE(source_id,event_id),
  FOREIGN KEY(incident_id) REFERENCES incidents(id) ON DELETE CASCADE,
  FOREIGN KEY(source_id) REFERENCES monitor_sources(id)
);
CREATE TABLE IF NOT EXISTS incident_evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER NOT NULL,
  evidence_type TEXT NOT NULL,
  source TEXT NOT NULL,
  supports TEXT NOT NULL DEFAULT '',
  contradicts TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  collected_at REAL NOT NULL,
  FOREIGN KEY(incident_id) REFERENCES incidents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_incident_evidence_incident
  ON incident_evidence(incident_id,collected_at);
CREATE TABLE IF NOT EXISTS incident_probe_locations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  location_type TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  token_enc TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  max_requests_per_hour INTEGER NOT NULL DEFAULT 12,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS incident_route_chains (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER NOT NULL,
  chain_id TEXT NOT NULL,
  happened_at REAL NOT NULL,
  anonymous_user_hash TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  UNIQUE(incident_id,chain_id),
  FOREIGN KEY(incident_id) REFERENCES incidents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_route_chains_expiry ON incident_route_chains(expires_at);
CREATE TABLE IF NOT EXISTS probe_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER NOT NULL,
  location_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  success INTEGER,
  latency_ms REAL,
  result_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL,
  finished_at REAL,
  FOREIGN KEY(incident_id) REFERENCES incidents(id) ON DELETE CASCADE,
  FOREIGN KEY(location_id) REFERENCES incident_probe_locations(id)
);
CREATE TABLE IF NOT EXISTS runner_pairing_codes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  runner_name TEXT NOT NULL,
  code_hash TEXT NOT NULL UNIQUE,
  expires_at REAL NOT NULL,
  used_at REAL,
  created_by INTEGER,
  created_at REAL NOT NULL,
  FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS paired_runners (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  encryption_public_key TEXT NOT NULL,
  signing_public_key TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'offline',
  version TEXT NOT NULL DEFAULT '',
  capabilities_json TEXT NOT NULL DEFAULT '{}',
  last_seen_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_paired_runners_status ON paired_runners(status,last_seen_at);
CREATE TABLE IF NOT EXISTS runner_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL UNIQUE,
  runner_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  encrypted_credentials_json TEXT NOT NULL,
  job_config_json TEXT NOT NULL,
  result_nonce TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}',
  telemetry_json TEXT NOT NULL DEFAULT '{}',
  lease_expires_at REAL,
  claimed_at REAL,
  started_at REAL,
  finished_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id),
  FOREIGN KEY(runner_id) REFERENCES paired_runners(id)
);
CREATE INDEX IF NOT EXISTS idx_runner_jobs_claim
  ON runner_jobs(runner_id,status,created_at);
CREATE TABLE IF NOT EXISTS online_verification_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  endpoint TEXT NOT NULL,
  token_enc TEXT NOT NULL DEFAULT '',
  protocol_version TEXT NOT NULL DEFAULT '1',
  enabled INTEGER NOT NULL DEFAULT 1,
  last_checked_at REAL,
  last_error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_launches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id INTEGER NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'tested',
  confirmed_by INTEGER,
  confirmed_at REAL,
  config_draft_json TEXT NOT NULL DEFAULT '{}',
  verification_source_id INTEGER,
  verification_json TEXT NOT NULL DEFAULT '{}',
  verified_at REAL,
  owner_note TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  FOREIGN KEY(channel_id) REFERENCES channels(id),
  FOREIGN KEY(confirmed_by) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY(verification_source_id) REFERENCES online_verification_sources(id)
);
CREATE TABLE IF NOT EXISTS feishu_bitable_settings (
  id INTEGER PRIMARY KEY CHECK(id=1),
  app_id TEXT NOT NULL,
  app_secret_enc TEXT NOT NULL,
  base_token TEXT NOT NULL,
  channel_table_id TEXT NOT NULL,
  model_table_id TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS external_sync_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  channel_id INTEGER NOT NULL,
  launch_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'preview',
  desired_json TEXT NOT NULL,
  remote_json TEXT NOT NULL DEFAULT '{}',
  diff_json TEXT NOT NULL DEFAULT '{}',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL,
  last_error TEXT NOT NULL DEFAULT '',
  confirmed_by INTEGER,
  confirmed_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  finished_at REAL,
  FOREIGN KEY(channel_id) REFERENCES channels(id),
  FOREIGN KEY(launch_id) REFERENCES channel_launches(id),
  FOREIGN KEY(confirmed_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_external_sync_jobs_due
  ON external_sync_jobs(status,next_attempt_at);
CREATE TABLE IF NOT EXISTS external_sync_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  table_kind TEXT NOT NULL,
  business_id TEXT NOT NULL,
  remote_record_id TEXT NOT NULL,
  last_synced_json TEXT NOT NULL,
  synced_at REAL NOT NULL,
  UNIQUE(provider,table_kind,business_id)
);
CREATE TABLE IF NOT EXISTS specialty_pack_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  item_count INTEGER NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  published_at REAL NOT NULL,
  UNIQUE(code,version)
);
CREATE TABLE IF NOT EXISTS test_plan_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id INTEGER NOT NULL,
  platform_group_id INTEGER NOT NULL,
  upstream_multiplier REAL NOT NULL,
  models_json TEXT NOT NULL,
  selected_specialties_json TEXT NOT NULL DEFAULT '[]',
  recommended_specialties_json TEXT NOT NULL DEFAULT '[]',
  estimate_json TEXT NOT NULL,
  created_by INTEGER,
  created_at REAL NOT NULL,
  FOREIGN KEY(channel_id) REFERENCES channels(id),
  FOREIGN KEY(platform_group_id) REFERENCES platform_groups(id),
  FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS target_usage_labels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  target_id INTEGER NOT NULL,
  usage_profile TEXT NOT NULL,
  pack_version TEXT NOT NULL,
  status TEXT NOT NULL,
  score REAL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  UNIQUE(task_id,usage_profile),
  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
  FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_usage_labels_target
  ON target_usage_labels(target_id,usage_profile,created_at);
CREATE TABLE IF NOT EXISTS model_aliases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  family_id INTEGER NOT NULL,
  model_id INTEGER NOT NULL,
  alias TEXT NOT NULL COLLATE NOCASE,
  created_by INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(family_id,alias),
  FOREIGN KEY(family_id) REFERENCES model_families(id),
  FOREIGN KEY(model_id) REFERENCES model_family_models(id),
  FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_model_aliases_model ON model_aliases(model_id);
CREATE TABLE IF NOT EXISTS model_lifecycle_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  object_type TEXT NOT NULL,
  object_id INTEGER NOT NULL,
  from_status TEXT NOT NULL,
  to_status TEXT NOT NULL,
  replacement_model_id INTEGER,
  reason TEXT NOT NULL,
  created_by INTEGER,
  actor TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  FOREIGN KEY(replacement_model_id) REFERENCES model_family_models(id),
  FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_model_lifecycle_history_object
  ON model_lifecycle_history(object_type,object_id,created_at);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL COLLATE NOCASE UNIQUE,
  display_name TEXT NOT NULL DEFAULT '',
  password_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  last_login_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  csrf_hash TEXT NOT NULL,
  created_at REAL NOT NULL,
  last_seen_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  revoked_at REAL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at,revoked_at);
CREATE TABLE IF NOT EXISTS login_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_hash TEXT NOT NULL,
  succeeded INTEGER NOT NULL DEFAULT 0,
  attempted_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_subject
  ON login_attempts(subject_hash,attempted_at);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  actor TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  object_type TEXT NOT NULL DEFAULT '',
  object_id TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL,
  request_id TEXT NOT NULL DEFAULT '',
  ip TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_events_created ON audit_events(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_events_user ON audit_events(user_id,created_at);
CREATE TABLE IF NOT EXISTS user_roles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  scope_type TEXT NOT NULL DEFAULT 'global',
  scope_id TEXT NOT NULL DEFAULT '*',
  granted_by INTEGER,
  reason TEXT NOT NULL,
  created_at REAL NOT NULL,
  revoked_at REAL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(granted_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_roles_active
  ON user_roles(user_id,role,scope_type,scope_id) WHERE revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS channel_configurations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_configuration_id INTEGER,
  legacy_channel_id INTEGER NOT NULL UNIQUE,
  family_id INTEGER NOT NULL,
  channel_name TEXT NOT NULL,
  display_name TEXT NOT NULL,
  protocol TEXT NOT NULL,
  base_url TEXT NOT NULL,
  key_enc TEXT NOT NULL,
  credential_fingerprint TEXT NOT NULL,
  upstream_multiplier REAL NOT NULL,
  route TEXT NOT NULL DEFAULT '',
  group_name TEXT NOT NULL DEFAULT '',
  configuration_fingerprint TEXT NOT NULL UNIQUE,
  business_status TEXT NOT NULL DEFAULT 'pending_test',
  note TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  FOREIGN KEY(source_configuration_id) REFERENCES channel_configurations(id),
  FOREIGN KEY(legacy_channel_id) REFERENCES channels(id),
  FOREIGN KEY(family_id) REFERENCES model_families(id)
);
CREATE INDEX IF NOT EXISTS idx_channel_configurations_family
  ON channel_configurations(family_id,upstream_multiplier,business_status);
CREATE TABLE IF NOT EXISTS configuration_model_mappings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  configuration_id INTEGER NOT NULL,
  canonical_model TEXT NOT NULL,
  request_model TEXT NOT NULL,
  legacy_target_id INTEGER NOT NULL UNIQUE,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(configuration_id,canonical_model),
  FOREIGN KEY(configuration_id) REFERENCES channel_configurations(id) ON DELETE CASCADE,
  FOREIGN KEY(legacy_target_id) REFERENCES targets(id)
);
CREATE INDEX IF NOT EXISTS idx_configuration_mappings_configuration
  ON configuration_model_mappings(configuration_id,sort_order,id);
CREATE TABLE IF NOT EXISTS configuration_audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  configuration_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  previous_value_json TEXT NOT NULL DEFAULT '{}',
  next_value_json TEXT NOT NULL DEFAULT '{}',
  reason_code TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  user_id INTEGER,
  actor TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  FOREIGN KEY(configuration_id) REFERENCES channel_configurations(id) ON DELETE CASCADE,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_configuration_audits_configuration
  ON configuration_audits(configuration_id,created_at);
CREATE TABLE IF NOT EXISTS configuration_connectivity_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  configuration_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  reason_code TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  initiated_by INTEGER,
  initiated_actor TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL,
  finished_at REAL,
  canceled_at REAL,
  FOREIGN KEY(configuration_id) REFERENCES channel_configurations(id) ON DELETE CASCADE,
  FOREIGN KEY(initiated_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_configuration_checks_configuration
  ON configuration_connectivity_checks(configuration_id,started_at DESC);
CREATE TABLE IF NOT EXISTS configuration_connectivity_check_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  check_id INTEGER NOT NULL,
  mapping_id INTEGER NOT NULL,
  canonical_model TEXT NOT NULL,
  request_model TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  http_status INTEGER,
  error_text TEXT NOT NULL DEFAULT '',
  started_at REAL,
  finished_at REAL,
  FOREIGN KEY(check_id) REFERENCES configuration_connectivity_checks(id) ON DELETE CASCADE,
  FOREIGN KEY(mapping_id) REFERENCES configuration_model_mappings(id)
);
CREATE INDEX IF NOT EXISTS idx_configuration_check_items_check
  ON configuration_connectivity_check_items(check_id,id);
CREATE TABLE IF NOT EXISTS configuration_fidelity_truths (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  configuration_id INTEGER NOT NULL,
  configuration_fingerprint TEXT NOT NULL,
  source_task_id INTEGER NOT NULL,
  source_manifest_root TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT NOT NULL,
  user_id INTEGER,
  actor TEXT NOT NULL,
  valid_from REAL NOT NULL,
  expires_at REAL NOT NULL,
  revoked_at REAL,
  replaced_by INTEGER,
  created_at REAL NOT NULL,
  FOREIGN KEY(configuration_id) REFERENCES channel_configurations(id),
  FOREIGN KEY(source_task_id) REFERENCES paired_tasks(task_id),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_configuration_truth_lookup
  ON configuration_fidelity_truths(configuration_id,status,expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_configuration_truth_active
  ON configuration_fidelity_truths(configuration_id)
  WHERE status='active' AND revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS admission_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  configuration_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  created_by INTEGER NOT NULL,
  current_conclusion_version INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  closed_at REAL,
  FOREIGN KEY(configuration_id) REFERENCES channel_configurations(id),
  FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_admission_batches_configuration
  ON admission_batches(configuration_id,created_at DESC);
CREATE TABLE IF NOT EXISTS admission_batch_models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  mapping_id INTEGER NOT NULL,
  canonical_model TEXT NOT NULL,
  request_model TEXT NOT NULL,
  benchmark_configuration_id INTEGER NOT NULL,
  benchmark_mapping_id INTEGER NOT NULL,
  benchmark_target_id INTEGER NOT NULL,
  current_task_id INTEGER,
  current_report_task_id INTEGER,
  current_report_version INTEGER,
  current_report_at REAL,
  report_invalidated_at REAL,
  status TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(batch_id,canonical_model),
  FOREIGN KEY(batch_id) REFERENCES admission_batches(id) ON DELETE CASCADE,
  FOREIGN KEY(mapping_id) REFERENCES configuration_model_mappings(id),
  FOREIGN KEY(benchmark_configuration_id) REFERENCES channel_configurations(id),
  FOREIGN KEY(benchmark_mapping_id) REFERENCES configuration_model_mappings(id),
  FOREIGN KEY(benchmark_target_id) REFERENCES targets(id),
  FOREIGN KEY(current_task_id) REFERENCES paired_tasks(task_id),
  FOREIGN KEY(current_report_task_id) REFERENCES paired_tasks(task_id)
);
CREATE INDEX IF NOT EXISTS idx_admission_batch_models_batch
  ON admission_batch_models(batch_id,id);
CREATE TABLE IF NOT EXISTS admission_batch_conclusions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  conclusion_version INTEGER NOT NULL,
  verdict TEXT NOT NULL,
  reason TEXT NOT NULL,
  evidence_refs_json TEXT NOT NULL,
  report_versions_json TEXT NOT NULL,
  self_review INTEGER NOT NULL,
  user_id INTEGER,
  actor TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(batch_id,conclusion_version),
  FOREIGN KEY(batch_id) REFERENCES admission_batches(id) ON DELETE CASCADE,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS paired_tasks (
  task_id INTEGER PRIMARY KEY,
  parent_task_id INTEGER,
  candidate_target_id INTEGER NOT NULL,
  benchmark_target_id INTEGER,
  created_by INTEGER,
  state TEXT NOT NULL,
  state_version INTEGER NOT NULL DEFAULT 1,
  idempotency_key TEXT NOT NULL,
  ready_at REAL,
  awaiting_truth_at REAL,
  fidelity_last_completed_at REAL,
  fidelity_manifest_root TEXT,
  task_manifest_root TEXT,
  truth_definition_id INTEGER,
  active_seconds REAL NOT NULL DEFAULT 0,
  formal INTEGER NOT NULL DEFAULT 1,
  secure_snapshot_ciphertext TEXT NOT NULL,
  asset_version TEXT NOT NULL,
  asset_hash TEXT NOT NULL,
  adapter_versions_json TEXT NOT NULL,
  tokenizer_version TEXT NOT NULL,
  grader_version TEXT NOT NULL,
  raw_expires_at REAL NOT NULL,
  structured_expires_at REAL NOT NULL,
  integrity_status TEXT NOT NULL DEFAULT 'unverified',
  integrity_error TEXT NOT NULL DEFAULT '',
  report_version INTEGER NOT NULL DEFAULT 0,
  request_limit INTEGER,
  token_limit INTEGER,
  money_limit REAL,
  requests_used INTEGER NOT NULL DEFAULT 0,
  tokens_used INTEGER NOT NULL DEFAULT 0,
  money_used REAL NOT NULL DEFAULT 0,
  stop_reason TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
  FOREIGN KEY(parent_task_id) REFERENCES paired_tasks(task_id),
  FOREIGN KEY(candidate_target_id) REFERENCES targets(id),
  FOREIGN KEY(benchmark_target_id) REFERENCES targets(id),
  FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_paired_tasks_state ON paired_tasks(state,ready_at);
CREATE TABLE IF NOT EXISTS paired_commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER,
  command_name TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '',
  result_state_version INTEGER,
  created_by INTEGER NOT NULL,
  created_at REAL NOT NULL,
  completed_at REAL,
  UNIQUE(created_by,idempotency_key),
  FOREIGN KEY(task_id) REFERENCES paired_tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_paired_commands_task
  ON paired_commands(task_id,created_at);
CREATE TABLE IF NOT EXISTS paired_fidelity_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  value TEXT NOT NULL,
  reason TEXT NOT NULL,
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  user_id INTEGER,
  actor TEXT NOT NULL,
  formal INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  FOREIGN KEY(task_id) REFERENCES paired_tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_paired_fidelity_decisions_task
  ON paired_fidelity_decisions(task_id,created_at);
CREATE TABLE IF NOT EXISTS paired_fidelity_truths (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_id INTEGER NOT NULL,
  channel_id INTEGER,
  upstream_multiplier REAL NOT NULL,
  fidelity_fingerprint TEXT NOT NULL,
  source_task_id INTEGER NOT NULL,
  source_manifest_root TEXT NOT NULL,
  user_id INTEGER,
  actor TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT NOT NULL,
  valid_from REAL NOT NULL,
  expires_at REAL NOT NULL,
  revoked_at REAL,
  replaced_by INTEGER,
  formal INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  FOREIGN KEY(target_id) REFERENCES targets(id),
  FOREIGN KEY(source_task_id) REFERENCES paired_tasks(task_id),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_paired_truth_lookup
  ON paired_fidelity_truths(target_id,upstream_multiplier,status,expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_paired_truth_active
  ON paired_fidelity_truths(target_id,upstream_multiplier)
  WHERE status='active' AND revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS paired_raw_blocks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  block_type TEXT NOT NULL,
  key_ciphertext TEXT NOT NULL,
  ciphertext TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  deleted_at REAL,
  FOREIGN KEY(task_id) REFERENCES paired_tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_paired_raw_expiry
  ON paired_raw_blocks(expires_at,deleted_at);
CREATE TABLE IF NOT EXISTS paired_evidence_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  record_seq INTEGER NOT NULL,
  record_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  raw_block_id INTEGER,
  previous_record_hash TEXT NOT NULL,
  record_hash TEXT NOT NULL,
  record_hmac TEXT NOT NULL,
  wall_time REAL NOT NULL,
  monotonic_time REAL NOT NULL,
  retention_until REAL NOT NULL,
  UNIQUE(task_id,record_seq),
  FOREIGN KEY(task_id) REFERENCES paired_tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(raw_block_id) REFERENCES paired_raw_blocks(id)
);
CREATE INDEX IF NOT EXISTS idx_paired_evidence_task
  ON paired_evidence_records(task_id,record_seq);
CREATE TABLE IF NOT EXISTS paired_resource_locks (
  resource_fingerprint TEXT PRIMARY KEY,
  task_id INTEGER NOT NULL,
  acquired_at REAL NOT NULL,
  lease_until REAL NOT NULL,
  FOREIGN KEY(task_id) REFERENCES paired_tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_paired_resource_task ON paired_resource_locks(task_id);
CREATE TABLE IF NOT EXISTS configuration_execution_locks (
  configuration_fingerprint TEXT PRIMARY KEY,
  task_id INTEGER NOT NULL,
  task_kind TEXT NOT NULL,
  acquired_at REAL NOT NULL,
  lease_until REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_configuration_execution_locks_task
  ON configuration_execution_locks(task_id,task_kind);
CREATE TABLE IF NOT EXISTS paired_report_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  parent_version INTEGER,
  reason TEXT NOT NULL,
  report_json TEXT NOT NULL,
  input_manifest_root TEXT NOT NULL,
  algorithm_versions_json TEXT NOT NULL,
  report_hash TEXT NOT NULL,
  created_by INTEGER,
  created_at REAL NOT NULL,
  UNIQUE(task_id,version),
  FOREIGN KEY(task_id) REFERENCES paired_tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS paired_conclusions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  report_version INTEGER NOT NULL,
  conclusion_version INTEGER NOT NULL,
  verdict TEXT NOT NULL,
  reason TEXT NOT NULL,
  evidence_refs_json TEXT NOT NULL,
  self_review INTEGER NOT NULL,
  user_id INTEGER,
  actor TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(task_id,report_version,conclusion_version),
  FOREIGN KEY(task_id) REFERENCES paired_tasks(task_id) ON DELETE CASCADE,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);"""

# 后加的列，老库也能平滑升上来
MIGRATIONS = [
    ("targets", "benchmark_id", "INTEGER"),
    ("tasks", "benchmark_id", "INTEGER"),
    ("targets", "group_id", "INTEGER"),
    ("targets", "pool", "TEXT DEFAULT 'ungrouped'"),   # ungrouped/trial/grouped
    # 硬题开关。存在任务行上而不是只看全局配置：重试同一个任务时口径不变，历史才可比
    ("tasks", "include_hard", "INTEGER"),
    ("targets", "hard_baseline", "TEXT DEFAULT ''"),   # 硬题纵向基线快照 JSON
    ("benchmarks", "hard", "TEXT DEFAULT ''"),         # 标杆里的硬题分快照 JSON
    ("targets", "channel_id", "INTEGER"),
    ("tasks", "selected_benchmark_id", "INTEGER"),
    ("tasks", "selected_benchmark_comparison", "TEXT DEFAULT ''"),
    ("targets", "upstream_multiplier", "REAL"),
    ("channels", "family_id", "INTEGER"),
    ("targets", "platform_group_id", "INTEGER"),
    ("benchmarks", "platform_group_id", "INTEGER"),
    ("benchmarks", "benchmark_model", "TEXT DEFAULT ''"),
    ("benchmarks", "superseded_at", "REAL"),
    ("channels", "lifecycle_status", "TEXT NOT NULL DEFAULT 'candidate'"),
    ("channels", "archived_at", "REAL"),
    ("model_family_models", "lifecycle_status", "TEXT NOT NULL DEFAULT 'enabled'"),
    ("model_family_models", "replacement_model_id", "INTEGER"),
    ("model_family_models", "archived_at", "REAL"),
    ("targets", "archived_at", "REAL"),
    ("model_families", "archived_at", "REAL"),
    ("platform_groups", "archived_at", "REAL"),
    ("channels", "business_id", "TEXT"),
    ("recommendations", "target_platform_group_id", "INTEGER"),
    ("recommendations", "suggested_platform_group_id", "INTEGER"),
    ("recommendations", "decided_platform_group_id", "INTEGER"),
    ("tasks", "attempt_count", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "max_attempts", "INTEGER NOT NULL DEFAULT 3"),
    ("tasks", "next_attempt_at", "REAL"),
    ("tasks", "last_executor_error", "TEXT DEFAULT ''"),
    ("tasks", "dead_lettered_at", "REAL"),
    ("tasks", "tags_json", "TEXT DEFAULT '[]'"),
    ("tasks", "owner", "TEXT DEFAULT ''"),
    ("tasks", "review_status", "TEXT DEFAULT 'unreviewed'"),
    ("tasks", "operator_note", "TEXT DEFAULT ''"),
    ("targets", "exact_configuration_id", "INTEGER"),
    ("targets", "canonical_model", "TEXT DEFAULT ''"),
    ("targets", "route", "TEXT DEFAULT ''"),
    ("paired_tasks", "truth_definition_kind", "TEXT NOT NULL DEFAULT 'legacy_target'"),
    ("paired_tasks", "admission_batch_model_id", "INTEGER"),
    ("scheduled_tests", "paused_at", "REAL"),
    ("scheduled_tests", "pause_reason", "TEXT NOT NULL DEFAULT ''"),
    ("scheduled_tests", "missed_runs", "INTEGER NOT NULL DEFAULT 0"),
    ("scheduled_runs", "plan_version_id", "INTEGER"),
    ("scheduled_runs", "configuration_snapshot_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("scheduled_measurement_evidence", "record_hmac", "TEXT NOT NULL DEFAULT ''"),
    ("scheduled_measurement_evidence", "raw_block_id", "INTEGER"),
]

_conn: sqlite3.Connection | None = None
_db_lock = threading.RLock()


def init() -> None:
    global _conn
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.execute("PRAGMA journal_mode=WAL")
    _backup_before_platform_workspace()
    _conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
    try:
        _migrate()
        _seed_rate_groups()
        _seed_model_families()
        _seed_usage_profiles()
        _seed_scheduled_primary_models()
        _migrate_platform_workspace()
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_targets_channel ON targets(channel_id)")
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_targets_platform_group "
            "ON targets(platform_group_id)")
        _backfill_channels()
        _backfill_business_ids()
        _backfill_upstream_multipliers()
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


def _backup_before_platform_workspace() -> None:
    assert _conn is not None
    if DB_PATH.name != "platform.db":
        return
    exists = _conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='platform_groups'"
    ).fetchone()
    if exists:
        return
    backup_dir = DB_PATH.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"platform-before-workspace-v2-{stamp}.db"
    destination = sqlite3.connect(backup_path)
    try:
        _conn.backup(destination)
    finally:
        destination.close()


def _seed_rate_groups() -> None:
    assert _conn is not None
    now = time.time()
    _conn.executemany(
        "INSERT OR IGNORE INTO rate_groups (multiplier,created_at) VALUES (?,?)",
        ((multiplier, now) for multiplier in DEFAULT_RATE_MULTIPLIERS),
    )
    _conn.execute(
        "INSERT OR IGNORE INTO rate_groups (multiplier,created_at) "
        "SELECT DISTINCT multiplier, ? FROM groups",
        (now,),
    )


def _seed_model_families() -> None:
    assert _conn is not None
    now = time.time()
    defaults = {
        "Claude": (
            ("claude-fable-5", "Fable 5", 10),
            ("claude-opus-5", "Opus 5", 20),
            ("claude-sonnet-5", "Sonnet 5", 30),
        ),
        "Codex": (
            ("gpt-5.6-sol", "GPT-5.6 Sol", 10),
            ("gpt-5.6-terra", "GPT-5.6 Terra", 20),
        ),
    }
    for family_name, models in defaults.items():
        _conn.execute(
            "INSERT OR IGNORE INTO model_families (name,created_at,updated_at) "
            "VALUES (?,?,?)", (family_name, now, now))
        family_id = _conn.execute(
            "SELECT id FROM model_families WHERE name=?", (family_name,)
        ).fetchone()["id"]
        for model, display_name, sort_order in models:
            _conn.execute(
                "INSERT OR IGNORE INTO model_family_models "
                "(family_id,model,display_name,enabled,sort_order,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (family_id, model, display_name, 1, sort_order, now, now),
            )


def _seed_usage_profiles() -> None:
    assert _conn is not None
    now = time.time()
    profiles = (
        ("general", "通用", 0, "未标注或通用请求"),
        ("agent", "Agent", 1, "多轮代理、工具调用和长链路任务"),
        ("coding", "编程", 1, "代码生成、理解和调试"),
        ("customer_service", "客服", 1, "低延迟、高并发客服交互"),
    )
    _conn.executemany(
        "INSERT OR IGNORE INTO usage_profiles "
        "(code,name,critical,description,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ((*profile, now, now) for profile in profiles),
    )


def _seed_scheduled_primary_models() -> None:
    assert _conn is not None
    now = time.time()
    defaults = (
        ("gpt-5.6-sol", 10),
        ("claude-opus-5", 20),
        ("claude-sonnet-5", 30),
    )
    _conn.executemany(
        "INSERT OR IGNORE INTO scheduled_primary_models "
        "(canonical_model,enabled,sort_order,updated_at) VALUES (?,?,?,?)",
        ((model, 1, sort_order, now) for model, sort_order in defaults),
    )


def _family_name_for_source(name: str) -> str:
    value = name.strip()
    key = value.casefold()
    if "claude" in key:
        return "Claude"
    if "codex" in key or key.startswith("gpt-5.6"):
        return "Codex"
    return value


def _legacy_group_model(name: str, multiplier: float) -> str:
    rate = format(float(multiplier), "g")
    return re.sub(
        rf"(?:\s*[-·]\s*)?{re.escape(rate)}\s*x\s*(?:分?组)?$",
        "", (name or "").strip(), flags=re.I,
    ).strip(" -·")


def _ensure_platform_slots(platform_group_id: int, family_id: int, now: float) -> None:
    assert _conn is not None
    models = _conn.execute(
        "SELECT model FROM model_family_models WHERE family_id=? ORDER BY sort_order,id",
        (family_id,),
    ).fetchall()
    _conn.executemany(
        "INSERT OR IGNORE INTO platform_group_benchmarks "
        "(platform_group_id,model,created_at,updated_at) VALUES (?,?,?,?)",
        ((platform_group_id, row["model"], now, now) for row in models),
    )


def _migrate_platform_workspace() -> None:
    assert _conn is not None
    marker = _conn.execute(
        "SELECT value FROM schema_meta WHERE key='platform_workspace_version'"
    ).fetchone()
    if marker:
        return
    now = time.time()
    scheduled_groups = _conn.execute(
        "SELECT * FROM scheduled_report_groups ORDER BY id"
    ).fetchall()
    scheduled_to_platform: dict[int, int] = {}
    for scheduled in scheduled_groups:
        family_name = _family_name_for_source(scheduled["model_family"])
        _conn.execute(
            "INSERT OR IGNORE INTO model_families (name,created_at,updated_at) "
            "VALUES (?,?,?)", (family_name, now, now))
        family_id = _conn.execute(
            "SELECT id FROM model_families WHERE name=?", (family_name,)
        ).fetchone()["id"]
        member_models = _conn.execute(
            "SELECT DISTINCT targets.model FROM targets "
            "JOIN scheduled_report_group_targets membership ON membership.target_id=targets.id "
            "WHERE membership.group_id=?", (scheduled["id"],)
        ).fetchall()
        for member in member_models:
            _conn.execute(
                "INSERT OR IGNORE INTO model_family_models "
                "(family_id,model,display_name,enabled,sort_order,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (family_id, member["model"], member["model"], 1, 100, now, now),
            )
        _conn.execute(
            "INSERT OR IGNORE INTO platform_groups "
            "(family_id,multiplier,created_at,updated_at) VALUES (?,?,?,?)",
            (family_id, scheduled["multiplier"], now, now),
        )
        platform_group_id = _conn.execute(
            "SELECT id FROM platform_groups WHERE family_id=? "
            "AND ABS(multiplier-?)<0.000000001",
            (family_id, scheduled["multiplier"]),
        ).fetchone()["id"]
        scheduled_to_platform[scheduled["id"]] = platform_group_id
        _ensure_platform_slots(platform_group_id, family_id, now)

    memberships = _conn.execute(
        "SELECT target_id,MIN(group_id) group_id,COUNT(*) memberships "
        "FROM scheduled_report_group_targets GROUP BY target_id"
    ).fetchall()
    for membership in memberships:
        if membership["memberships"] != 1:
            continue
        platform_group_id = scheduled_to_platform.get(membership["group_id"])
        if platform_group_id:
            _conn.execute(
                "UPDATE targets SET platform_group_id=? WHERE id=?",
                (platform_group_id, membership["target_id"]),
            )

    benchmark_groups = _conn.execute(
        "SELECT * FROM groups WHERE benchmark_id IS NOT NULL ORDER BY id"
    ).fetchall()
    for legacy_group in benchmark_groups:
        model = _legacy_group_model(legacy_group["name"], legacy_group["multiplier"])
        benchmark_source = _conn.execute(
            "SELECT tasks.target_id,targets.model,targets.platform_group_id "
            "FROM benchmarks JOIN tasks ON tasks.id=benchmarks.source_task_id "
            "JOIN targets ON targets.id=tasks.target_id WHERE benchmarks.id=?",
            (legacy_group["benchmark_id"],),
        ).fetchone()
        if not benchmark_source or not benchmark_source["platform_group_id"]:
            continue
        if benchmark_source["model"] != model:
            continue
        matches = _conn.execute(
            "SELECT platform_group_benchmarks.id slot_id,platform_groups.id platform_group_id "
            "FROM platform_group_benchmarks JOIN platform_groups "
            "ON platform_groups.id=platform_group_benchmarks.platform_group_id "
            "WHERE platform_group_benchmarks.platform_group_id=? "
            "AND platform_group_benchmarks.model=? "
            "AND ABS(platform_groups.multiplier-?)<0.000000001",
            (benchmark_source["platform_group_id"], model, legacy_group["multiplier"]),
        ).fetchall()
        if len(matches) != 1:
            continue
        slot = matches[0]
        occupied = _conn.execute(
            "SELECT benchmark_id FROM platform_group_benchmarks WHERE id=?",
            (slot["slot_id"],),
        ).fetchone()["benchmark_id"]
        if occupied:
            continue
        _conn.execute(
            "UPDATE platform_group_benchmarks SET benchmark_id=?,updated_at=? WHERE id=?",
            (legacy_group["benchmark_id"], now, slot["slot_id"]),
        )
        _conn.execute(
            "UPDATE benchmarks SET platform_group_id=?,benchmark_model=? WHERE id=?",
            (slot["platform_group_id"], model, legacy_group["benchmark_id"]),
        )

    channel_rows = _conn.execute("SELECT id FROM channels ORDER BY id").fetchall()
    for channel in channel_rows:
        family_ids = _conn.execute(
            "SELECT DISTINCT platform_groups.family_id FROM targets "
            "JOIN platform_groups ON platform_groups.id=targets.platform_group_id "
            "WHERE targets.channel_id=?", (channel["id"],)
        ).fetchall()
        if len(family_ids) == 1:
            _conn.execute(
                "UPDATE channels SET family_id=? WHERE id=?",
                (family_ids[0]["family_id"], channel["id"]),
            )
    _conn.execute(
        "INSERT INTO schema_meta (key,value) VALUES ('platform_workspace_version','2')"
    )


def _migrate() -> None:
    """补齐后加的列。已存在就跳过，可重复执行。"""
    assert _conn is not None
    for table, col, coltype in MIGRATIONS:
        cols = {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")


def _backfill_channels() -> None:
    assert _conn is not None
    rows = _conn.execute(
        "SELECT * FROM targets WHERE channel_id IS NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        existing = _conn.execute(
            "SELECT id FROM channels WHERE protocol=? AND base_url=? AND key_enc=?",
            (row["protocol"], row["base_url"], row["key_enc"]),
        ).fetchone()
        if existing:
            channel_id = existing["id"]
        else:
            now = row["created_at"] or time.time()
            cur = _conn.execute(
                "INSERT INTO channels "
                "(name,protocol,base_url,group_name,env,key_enc,source,edited_fields,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (row["group_name"] or row["name"], row["protocol"], row["base_url"],
                 row["group_name"], row["env"], row["key_enc"], row["source"],
                 row["edited_fields"], now, row["updated_at"] or now),
            )
            channel_id = cur.lastrowid
        _conn.execute("UPDATE targets SET channel_id=? WHERE id=?", (channel_id, row["id"]))


def _backfill_upstream_multipliers() -> None:
    assert _conn is not None
    _conn.execute(
        "UPDATE targets SET upstream_multiplier=(SELECT groups.multiplier FROM groups "
        "WHERE groups.id=targets.group_id) WHERE upstream_multiplier IS NULL AND group_id IS NOT NULL"
    )


def _backfill_business_ids() -> None:
    assert _conn is not None
    _conn.execute(
        "UPDATE channels SET business_id='channel-' || id "
        "WHERE business_id IS NULL OR business_id=''"
    )


@contextmanager
def cursor():
    assert _conn is not None, "store.init() 未调用"
    # SQLite 连接由 Web、调度器和后台采集线程共享；事务必须串行，
    # 否则一个线程的 commit 会撞上另一个线程正在结束的事务。
    with _db_lock:
        cur = _conn.cursor()
        try:
            yield cur
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise
        finally:
            cur.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def insert(table: str, data: dict[str, Any]) -> int:
    cols = ",".join(data)
    marks = ",".join("?" * len(data))
    with cursor() as c:
        c.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))
        return int(c.lastrowid or 0)


def update(
    table: str, row_id: int, data: dict[str, Any], *, key: str = "id",
) -> None:
    sets = ",".join(f"{k}=?" for k in data)
    with cursor() as c:
        c.execute(
            f"UPDATE {table} SET {sets} WHERE {key}=?", (*data.values(), row_id)
        )


def get(table: str, row_id: int, *, key: str = "id") -> dict[str, Any] | None:
    with cursor() as c:
        c.execute(f"SELECT * FROM {table} WHERE {key}=?", (row_id,))
        return row_to_dict(c.fetchone())


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with cursor() as c:
        c.execute(sql, params)
        return [dict(r) for r in c.fetchall()]


def execute(sql: str, params: tuple = ()) -> None:
    with cursor() as c:
        c.execute(sql, params)


def task_report(task: dict[str, Any], default: Any = None) -> Any:
    if task.get("report"):
        return loads(task["report"], default)
    rows = query(
        "SELECT report_gzip FROM task_report_archives WHERE task_id=?", (task["id"],)
    )
    if not rows:
        return default
    try:
        return json.loads(gzip.decompress(rows[0]["report_gzip"]).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def archive_task_reports(older_than: float) -> dict[str, int]:
    rows = query(
        "SELECT id,report FROM tasks WHERE finished_at<? AND report!='' "
        "AND report IS NOT NULL ORDER BY id", (older_than,)
    )
    archived = original = compressed = 0
    for row in rows:
        raw = row["report"].encode("utf-8")
        packed = gzip.compress(raw, compresslevel=9)
        with cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO task_report_archives "
                "(task_id,report_gzip,original_bytes,compressed_bytes,archived_at) "
                "VALUES (?,?,?,?,?)",
                (row["id"], packed, len(raw), len(packed), time.time()),
            )
            if cur.rowcount:
                cur.execute("UPDATE tasks SET report='' WHERE id=?", (row["id"],))
                archived += 1
                original += len(raw)
                compressed += len(packed)
    return {"archived": archived, "original_bytes": original,
            "compressed_bytes": compressed}


def record_system_alert(
    dedupe_key: str, kind: str, title: str, detail: str,
    severity: str = "warning",
) -> None:
    """记录可查询、可去重的后台异常，避免调度错误被静默吞掉。"""
    now = time.time()
    with cursor() as c:
        c.execute(
            "INSERT INTO system_alerts "
            "(dedupe_key,kind,severity,status,title,detail,occurrence_count,first_seen_at,last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(dedupe_key) DO UPDATE SET "
            "severity=excluded.severity,status='open',title=excluded.title,"
            "detail=excluded.detail,occurrence_count=system_alerts.occurrence_count+1,"
            "last_seen_at=excluded.last_seen_at,resolved_at=NULL",
            (dedupe_key, kind, severity, "open", title, detail, 1, now, now),
        )


def delete(table: str, row_id: int) -> None:
    with cursor() as c:
        c.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))


# ---- JSON 字段读写小工具 ----

def loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---- 事件时间线 ----

def add_event(task_id: int, message: str, stage: str = "", level: str = "info") -> None:
    insert("events", {
        "task_id": task_id, "ts": time.time(),
        "level": level, "stage": stage, "message": message,
    })


def list_events(task_id: int) -> list[dict[str, Any]]:
    return query("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,))
