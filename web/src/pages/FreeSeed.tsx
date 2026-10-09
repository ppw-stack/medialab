import { useEffect, useMemo, useState } from "react";
import { Card, Table, Tag, Typography, Progress, Space, Button, message, Select, Input, Empty, Tooltip } from "antd";
import { api } from "../api";

interface FreeSeedCard {
  id: number; name: string; site: string; size: number;
  free_type: string | null; free_end_at: string | null;
  free_source: string | null; free_confidence: number | null;
  remaining_seconds: number | null; urgency: string;
  next_inspect_seconds: number; progress: number; downloaded: number;
  dlspeed: number; feasibility: string; eta_hours: number | null;
  seconds_to_abort: number | null; abort_at: string | null;
  paused_reason: string | null; status: string;
}

interface FreeSeedList { total: number; items: FreeSeedCard[]; }
interface OutcomeSummary {
  window_completed: number; late_completed: number; missed: number; aborted: number;
  window_completion_rate: number | null;
}

// 紧迫度配色（FR-3.4.1）
const urgencyColor: Record<string, string> = {
  normal: "green", notice: "blue", approaching: "gold",
  urgent: "volcano", critical: "magenta", expired: "red", permanent: "green",
};

const freeTypeLabel: Record<string, string> = {
  freeleech_timed: "限时免费", freeleech_permanent: "永久免费",
  twoup_timed: "限时 2X", neutral_leech: "中性种", unknown: "未知",
};

function formatRemaining(seconds: number | null): string {
  if (seconds === null) return "永久";
  if (seconds <= 0) return "已过期";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}天 ${h}小时`;
  if (h > 0) return `${h}小时 ${m}分`;
  const s = Math.floor(seconds % 60);
  if (m > 0) return `${m}分 ${s}秒`;
  return `${s}秒`;
}

const formatSize = (b: number) => {
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(0)} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(0)} MB`;
  return `${(b / 1024 ** 3).toFixed(1)} GB`;
};

export default function FreeSeed() {
  const [data, setData] = useState<FreeSeedList>({ total: 0, items: [] });
  const [summary, setSummary] = useState<OutcomeSummary | null>(null);
  const [urgencyFilter, setUrgencyFilter] = useState<string | undefined>(undefined);
  const [typeFilter, setTypeFilter] = useState<string | undefined>(undefined);
  const [q, setQ] = useState("");

  const loadData = () => {
    api.get<FreeSeedList>("/free-seed?in_flight_only=true&page_size=200")
      .then(setData)
      .catch(() => {});
    api.get<OutcomeSummary>("/free-seed/summary")
      .then(setSummary)
      .catch(() => {});
  };
  useEffect(() => {
    loadData();
    const t = setInterval(loadData, 10000);
    return () => clearInterval(t);
  }, []);

  const abort = async (id: number) => {
    try {
      await api.post(`/free-seed/${id}/abort`, { reason: "manual" });
      message.success("已提交中止");
      loadData();
    } catch (e) { message.error(e instanceof Error ? e.message : "中止失败"); }
  };

  // 客户端筛选（在已加载的 items 上做）— 后端 in_flight_only 已经把已结束的过滤了
  const filtered = useMemo(() => {
    let items = data.items;
    if (urgencyFilter) items = items.filter((r) => r.urgency === urgencyFilter);
    if (typeFilter) items = items.filter((r) => r.free_type === typeFilter);
    if (q) {
      const ql = q.toLowerCase();
      items = items.filter((r) => r.name.toLowerCase().includes(ql) || r.site.toLowerCase().includes(ql));
    }
    return items;
  }, [data.items, urgencyFilter, typeFilter, q]);

  // 按紧迫度排序
  const urgencyOrder: Record<string, number> = {
    expired: 0, critical: 1, urgent: 2, approaching: 3, notice: 4, normal: 5, permanent: 6,
  };
  const sortedItems = [...filtered].sort(
    (a, b) => (urgencyOrder[a.urgency] ?? 9) - (urgencyOrder[b.urgency] ?? 9),
  );

  const columns = [
    {
      title: "种子名",
      dataIndex: "name",
      key: "name",
      ellipsis: true,
      render: (v: string, r: FreeSeedCard) => (
        <Space direction="vertical" size={0}>
          <span style={{ fontWeight: 500 }}>{v}</span>
          <Typography.Text type="secondary" style={{ fontSize: 11 }}>{r.site}</Typography.Text>
        </Space>
      ),
    },
    {
      title: "类型", dataIndex: "free_type", key: "free_type",
      render: (v: string | null) => v ?
        <Tag color={v === "freeleech_permanent" ? "green" : "purple"}>{freeTypeLabel[v] || v}</Tag> : "-",
    },
    {
      title: "窗口来源", dataIndex: "free_source", key: "src",
      render: (v: string | null) => v ?
        <Tooltip title="confidence 越低越需关注"><Tag color="blue">{v}</Tag></Tooltip> : "-",
    },
    {
      title: "紧迫度",
      key: "urgency",
      render: (_: unknown, r: FreeSeedCard) => (
        <Tag color={urgencyColor[r.urgency] || "default"}>
          {r.urgency === "permanent" ? "永久" : formatRemaining(r.remaining_seconds)}
        </Tag>
      ),
    },
    {
      title: "进度",
      key: "progress",
      render: (_: unknown, r: FreeSeedCard) => (
        <Space direction="vertical" size={0} style={{ width: 140 }}>
          <Progress
            percent={Math.round(r.progress * 100)}
            size="small"
            status={r.feasibility === "ok" ? "normal" : r.feasibility === "critical" ? "exception" : "active"}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {r.feasibility === "ok" ? "✅" : r.feasibility === "critical" ? "⚠️ 临界" : "❌"}
            {r.eta_hours ? ` ${r.eta_hours}h` : ""}
          </Typography.Text>
        </Space>
      ),
    },
    { title: "大小", dataIndex: "size", key: "size", render: formatSize, width: 80 },
    {
      title: "距中止",
      key: "abort",
      render: (_: unknown, r: FreeSeedCard) => r.seconds_to_abort !== null ? formatRemaining(r.seconds_to_abort) : "-",
    },
    {
      title: "状态", dataIndex: "status", key: "status",
      render: (v: string) => <Tag color="default">{v}</Tag>,
    },
    {
      title: "操作", key: "action", render: (_: unknown, r: FreeSeedCard) => (
        <Button size="small" danger onClick={() => abort(r.id)}>中止</Button>
      ),
    },
  ];

  return (
    <div>
      <Typography.Title level={4}>免费种管理</Typography.Title>

      {summary && (
        <Space size="middle" wrap style={{ marginBottom: 16 }}>
          <Card size="small"><b>窗口内完成：</b>{summary.window_completed}</Card>
          <Card size="small"><b>边界超期：</b>{summary.late_completed}</Card>
          <Card size="small"><b>看护失效：</b>{summary.missed}</Card>
          <Card size="small"><b>已中止：</b>{summary.aborted}</Card>
          <Card size="small">
            <b>窗口内完成率：</b>
            {summary.window_completion_rate !== null
              ? `${(summary.window_completion_rate * 100).toFixed(1)}%`
              : "-"}
          </Card>
        </Space>
      )}

      <Card
        title={`在途任务（${data.total}）`}
        extra={
          <Space>
            <Select
              allowClear placeholder="紧迫度" style={{ width: 110 }} value={urgencyFilter}
              onChange={setUrgencyFilter}
              options={[
                { value: "critical", label: "🔴 临界" },
                { value: "urgent", label: "🟠 紧急" },
                { value: "approaching", label: "🟡 临近" },
                { value: "notice", label: "🔵 注意" },
                { value: "normal", label: "🟢 正常" },
                { value: "permanent", label: "♾️ 永久" },
              ]}
            />
            <Select
              allowClear placeholder="免费类型" style={{ width: 130 }} value={typeFilter}
              onChange={setTypeFilter}
              options={Object.entries(freeTypeLabel).map(([v, l]) => ({ value: v, label: l }))}
            />
            <Input.Search allowClear placeholder="搜索名称/站点" style={{ width: 200 }} onChange={(e) => setQ(e.target.value)} />
            <Button onClick={() => api.post("/free-seed/pull").then(loadData)}>
              手动抓取
            </Button>
            <Button onClick={() => api.post("/free-seed/watch").then(() => message.success("看护已执行"))}>
              手动看护
            </Button>
          </Space>
        }
      >
        {sortedItems.length === 0 ? (
          <Empty description="暂无在途免费种" />
        ) : (
          <Table
            dataSource={sortedItems}
            columns={columns}
            rowKey="id"
            pagination={false}
            size="middle"
          />
        )}
      </Card>
    </div>
  );
}
