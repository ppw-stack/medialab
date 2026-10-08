import { useEffect, useState } from "react";
import { Card, Col, Row, Statistic, Table, Tag, Typography, Progress, Space, Button, message } from "antd";
import { api } from "../api";

interface FreeSeedCard {
  id: number;
  name: string;
  site: string;
  size: number;
  free_type: string | null;
  free_end_at: string | null;
  free_source: string | null;
  free_confidence: number | null;
  remaining_seconds: number | null;
  urgency: string;
  next_inspect_seconds: number;
  progress: number;
  downloaded: number;
  dlspeed: number;
  feasibility: string;
  eta_hours: number | null;
  seconds_to_abort: number | null;
  abort_at: string | null;
  paused_reason: string | null;
  status: string;
}

interface FreeSeedList {
  total: number;
  items: FreeSeedCard[];
}

interface OutcomeSummary {
  window_completed: number;
  late_completed: number;
  missed: number;
  aborted: number;
  window_completion_rate: number | null;
}

const urgencyColor: Record<string, string> = {
  normal: "green",
  notice: "blue",
  approaching: "orange",
  urgent: "volcano",
  critical: "red",
  expired: "red",
  permanent: "green",
};

const freeTypeLabel: Record<string, string> = {
  freeleech_timed: "限时免费",
  freeleech_permanent: "永久免费",
  twoup_timed: "限时 2X",
  neutral_leech: "中性种",
  unknown: "未知",
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

export default function FreeSeed() {
  const [data, setData] = useState<FreeSeedList>({ total: 0, items: [] });
  const [summary, setSummary] = useState<OutcomeSummary | null>(null);

  const loadData = () => {
    api.get<FreeSeedList>("/free-seed").then(setData).catch(() => {});
    api.get<OutcomeSummary>("/free-seed/summary").then(setSummary).catch(() => {});
  };

  useEffect(() => {
    loadData();
    const timer = setInterval(loadData, 10000);
    return () => clearInterval(timer);
  }, []);

  const abort = async (id: number) => {
    try {
      await api.post(`/free-seed/${id}/abort`, { reason: "manual" });
      message.success("已提交中止");
      loadData();
    } catch (exc) {
      message.error(exc instanceof Error ? exc.message : "中止失败");
    }
  };

  const columns = [
    {
      title: "种子名",
      dataIndex: "name",
      key: "name",
      ellipsis: true,
    },
    {
      title: "免费类型",
      dataIndex: "free_type",
      key: "free_type",
      render: (v: string | null) => v ? <Tag>{freeTypeLabel[v] || v}</Tag> : "-",
    },
    {
      title: "窗口剩余",
      key: "remaining",
      render: (_: unknown, r: FreeSeedCard) => (
        <Tag color={urgencyColor[r.urgency] || "default"}>
          {formatRemaining(r.remaining_seconds)}
        </Tag>
      ),
    },
    {
      title: "进度",
      key: "progress",
      render: (_: unknown, r: FreeSeedCard) => (
        <Space direction="vertical" style={{ width: 120 }}>
          <Progress percent={Math.round(r.progress * 100)} size="small" />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {r.feasibility === "ok" ? "✅" : r.feasibility === "critical" ? "⚠️" : "❌"}
            {r.eta_hours ? ` ${r.eta_hours}h` : ""}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "距中止",
      key: "abort",
      render: (_: unknown, r: FreeSeedCard) =>
        r.seconds_to_abort !== null ? formatRemaining(r.seconds_to_abort) : "-",
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      render: (v: string) => <Tag>{v}</Tag>,
    },
    {
      title: "操作",
      key: "action",
      render: (_: unknown, r: FreeSeedCard) => (
        <Button size="small" danger onClick={() => abort(r.id)}>中止</Button>
      ),
    },
  ];

  return (
    <div>
      <Typography.Title level={4}>免费种管理</Typography.Title>

      {summary && (
        <Row gutter={16} style={{ marginBottom: 16 }}>
          <Col span={5}>
            <Card><Statistic title="窗口内完成" value={summary.window_completed} /></Card>
          </Col>
          <Col span={5}>
            <Card><Statistic title="边界超期完成" value={summary.late_completed} /></Card>
          </Col>
          <Col span={5}>
            <Card><Statistic title="看护失效" value={summary.missed} /></Card>
          </Col>
          <Col span={5}>
            <Card><Statistic title="已中止" value={summary.aborted} /></Card>
          </Col>
          <Col span={4}>
            <Card><Statistic
              title="窗口内完成率"
              value={summary.window_completion_rate !== null
                ? `${(summary.window_completion_rate * 100).toFixed(1)}%`
                : "-"}
            /></Card>
          </Col>
        </Row>
      )}

      <Card title={`在途任务（${data.total}）`}>
        <Table
          dataSource={data.items}
          columns={columns}
          rowKey="id"
          pagination={{ pageSize: 20 }}
        />
      </Card>

      <Space style={{ marginTop: 16 }}>
        <Button onClick={() => api.post("/free-seed/pull").then(loadData)}>
          手动抓取
        </Button>
        <Button onClick={() => api.post("/free-seed/watch").then(() => message.success("看护已执行"))}>
          手动看护
        </Button>
      </Space>
    </div>
  );
}
