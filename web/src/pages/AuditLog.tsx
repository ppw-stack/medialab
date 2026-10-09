import { useEffect, useState } from "react";
import {
  Card, Table, Tag, Select, Space, Button, DatePicker, Typography, message, Input, Tooltip,
} from "antd";
import { type Dayjs } from "dayjs";
import { api } from "../api";


interface AuditEntry {
  id: number; ts: string; action: string; trigger_source: string;
  hash_count: number; freed_space: number; result: string; reason: string | null;
}

interface CleanupLog {
  id: number; ts: string; action: string; trigger_source: string; rule_id: number | null;
  rule_name: string | null; affected_count: number; freed_space: number; result: string;
  error: string | null;
}

type Source = "all" | "action" | "cleanup";
const TRIGGER_COLORS: Record<string, string> = {
  schedule: "blue", manual: "purple", alert_linked: "orange",
  rule: "cyan", window_abort: "red", space_pressure: "magenta", report: "default",
};

const formatSize = (b: number) => {
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(0)} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(0)} MB`;
  return `${(b / 1024 ** 3).toFixed(1)} GB`;
};
const formatDate = (d: string) => new Date(d).toLocaleString();

export default function AuditLog() {
  const [source, setSource] = useState<Source>("action");
  const [trigger, setTrigger] = useState<string | undefined>(undefined);
  const [action, setAction] = useState<string | undefined>(undefined);
  const [q, setQ] = useState("");
  const [range, setRange] = useState<[Dayjs, Dayjs] | null>(null);
  const [data, setData] = useState<(AuditEntry | CleanupLog)[]>([]);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      let rows: (AuditEntry | CleanupLog)[];
      if (source === "cleanup") {
        const r = await api.get<CleanupLog[]>(`/cleanup/logs?limit=500`);
        rows = r;
      } else {
        const qs = new URLSearchParams();
        if (trigger) qs.set("trigger_source", trigger);
        if (action) qs.set("action", action);
        qs.set("limit", "500");
        const r = await api.get<{ items: AuditEntry[] }>(`/torrents/audit?${qs.toString()}`);
        rows = r.items;
      }
      let filtered = rows;
      if (range) {
        filtered = filtered.filter((e) => {
          const t = new Date(e.ts).getTime();
          return t >= range[0].valueOf() && t <= range[1].valueOf() + 86_400_000;
        });
      }
      if (q) {
        const ql = q.toLowerCase();
        filtered = filtered.filter((e) =>
          (e as { reason?: string }).reason?.toLowerCase().includes(ql) ||
          (e as { action?: string }).action?.toLowerCase().includes(ql) ||
          e.trigger_source.toLowerCase().includes(ql),
        );
      }
      setData(filtered);
    } finally { setLoading(false); }
  };
  useEffect(() => { load(); }, [source, trigger, action, range, q]);

  const exportCsv = () => {
    if (!data.length) { message.warning("无数据"); return; }
    const headers = ["ts", "action", "trigger_source", "result", "count", "freed_space", "rule_name", "reason"];
    const rows = data.map((e) => [
      e.ts, (e as { action: string }).action, e.trigger_source, e.result,
      (e as { hash_count?: number }).hash_count ?? (e as { affected_count?: number }).affected_count,
      e.freed_space,
      (e as { rule_name?: string }).rule_name || "",
      (e as { reason?: string }).reason || "",
    ]);
    const csv = [headers, ...rows].map((r) => r.map((c) => `"${c}"`).join(",")).join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `audit_${Date.now()}.csv`; a.click();
    URL.revokeObjectURL(url);
  };

  const columns = source === "cleanup"
    ? [
        { title: "时间", dataIndex: "ts", key: "ts", render: formatDate, width: 160 },
        { title: "动作", dataIndex: "action", key: "action" },
        { title: "规则", dataIndex: "rule_name", key: "rule", render: (v: string | null) => v || "-" },
        { title: "触发源", dataIndex: "trigger_source", key: "ts2", render: (v: string) => <Tag color={TRIGGER_COLORS[v] || "default"}>{v}</Tag> },
        { title: "任务数", dataIndex: "affected_count", key: "count" },
        { title: "释放", dataIndex: "freed_space", key: "free", render: formatSize },
        {
          title: "结果", dataIndex: "result", key: "r",
          render: (v: string) => <Tag color={v === "done" ? "green" : "red"}>{v}</Tag>,
        },
        { title: "错误", dataIndex: "error", key: "err", render: (v: string | null) => v ? <Tooltip title={v}><Tag color="red">有</Tag></Tooltip> : "-" },
      ]
    : [
        { title: "时间", dataIndex: "ts", key: "ts", render: formatDate, width: 160 },
        { title: "动作", dataIndex: "action", key: "action" },
        { title: "触发源", dataIndex: "trigger_source", key: "ts2", render: (v: string) => <Tag color={TRIGGER_COLORS[v] || "default"}>{v}</Tag> },
        { title: "数量", dataIndex: "hash_count", key: "count" },
        { title: "释放", dataIndex: "freed_space", key: "free", render: formatSize },
        {
          title: "结果", dataIndex: "result", key: "r",
          render: (v: string) => <Tag color={v === "ok" ? "green" : v === "error" ? "red" : "default"}>{v}</Tag>,
        },
        { title: "原因", dataIndex: "reason", key: "reason" },
      ];

  return (
    <div>
      <Typography.Title level={4}>审计日志</Typography.Title>
      <Card
        title={`${data.length} 条记录`}
        extra={
          <Space>
            <Select
              value={source}
              onChange={setSource}
              style={{ width: 130 }}
              options={[
                { value: "all", label: "操作审计" },
                { value: "cleanup", label: "清理审计" },
              ]}
            />
            <Select
              allowClear placeholder="触发源"
              style={{ width: 140 }}
              value={trigger}
              onChange={setTrigger}
              options={Object.entries(TRIGGER_COLORS).map(([v, c]) => ({ value: v, label: <Tag color={c}>{v}</Tag> }))}
            />
            {source === "all" && (
              <Select
                allowClear placeholder="动作"
                style={{ width: 140 }}
                value={action}
                onChange={setAction}
                options={[
                  { value: "add_torrent", label: "add_torrent" },
                  { value: "trash", label: "trash" },
                  { value: "remove", label: "remove" },
                ]}
              />
            )}
            <DatePicker.RangePicker
              value={range}
              onChange={(v) => setRange(v as [Dayjs, Dayjs] | null)}
              allowClear
            />
            <Input.Search allowClear placeholder="搜索原因/动作" style={{ width: 180 }} onChange={(e) => setQ(e.target.value)} />
            <Button onClick={exportCsv}>导出 CSV</Button>
          </Space>
        }
      >
        <Table
          loading={loading}
          dataSource={data}
          columns={columns}
          rowKey="id"
          pagination={{ pageSize: 30 }}
        />
      </Card>
    </div>
  );
}
