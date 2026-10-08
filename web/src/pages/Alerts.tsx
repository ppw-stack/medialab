import { useEffect, useState } from "react";
import { Card, Table, Tag, Typography, Button, Space, message } from "antd";
import { api } from "../api";

interface Alert {
  id: number;
  ts: string;
  level: string;
  category: string;
  info_hash: string | null;
  message: string;
  dedupe_key: string;
  acknowledged: boolean;
  muted_until: string | null;
}

interface AlertList {
  total: number;
  page: number;
  page_size: number;
  items: Alert[];
}

const levelColor: Record<string, string> = {
  critical: "red",
  warning: "orange",
  info: "blue",
};

export default function Alerts() {
  const [data, setData] = useState<AlertList>({ total: 0, page: 1, page_size: 50, items: [] });
  const [unackedOnly, setUnackedOnly] = useState(false);

  const load = () => {
    api.get<AlertList>(`/alerts?unacked_only=${unackedOnly}&page_size=100`).then(setData).catch(() => {});
  };

  useEffect(() => { load(); }, [unackedOnly]);

  const ack = async (id: number) => {
    await api.post(`/alerts/${id}/ack`);
    message.success("已确认");
    load();
  };

  const ackAll = async () => {
    await api.post(`/alerts/ack-all`);
    message.success("已全部确认");
    load();
  };

  const triggerInspect = async () => {
    const r = await api.post<{ findings: number; critical: number; warning: number }>("/alerts/inspect");
    message.success(`巡检完成：发现 ${r.findings} 条`);
    load();
  };

  const columns = [
    { title: "时间", dataIndex: "ts", key: "ts", render: (v: string) => new Date(v).toLocaleString(), width: 180 },
    {
      title: "级别",
      dataIndex: "level",
      key: "level",
      render: (v: string) => <Tag color={levelColor[v] || "default"}>{v}</Tag>,
    },
    { title: "分类", dataIndex: "category", key: "category" },
    { title: "消息", dataIndex: "message", key: "message", ellipsis: true },
    {
      title: "状态",
      key: "status",
      render: (_: unknown, r: Alert) => r.acknowledged ? <Tag color="green">已确认</Tag> : <Tag color="orange">未确认</Tag>,
    },
    {
      title: "操作",
      key: "action",
      render: (_: unknown, r: Alert) =>
        !r.acknowledged && <Button size="small" onClick={() => ack(r.id)}>确认</Button>,
    },
  ];

  return (
    <div>
      <Typography.Title level={4}>监控告警</Typography.Title>
      <Space style={{ marginBottom: 16 }}>
        <Button onClick={() => setUnackedOnly(!unackedOnly)}>
          {unackedOnly ? "显示全部" : "仅未确认"}
        </Button>
        <Button onClick={ackAll}>全部确认</Button>
        <Button type="primary" onClick={triggerInspect}>手动巡检</Button>
      </Space>
      <Card>
        <Table dataSource={data.items} columns={columns} rowKey="id" pagination={{ pageSize: 20 }} />
      </Card>
    </div>
  );
}
