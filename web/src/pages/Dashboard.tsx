import { useEffect, useState } from "react";
import { Card, Col, Row, Statistic, Table, Tag, Typography } from "antd";
import { api } from "../api";

interface DashboardData {
  sites: { total: number; enabled: number };
  torrents: { by_status: Record<string, number>; by_source: Record<string, number> };
  free_seed: { in_flight: number };
}

const statusColor: Record<string, string> = {
  active: "green",
  paused: "orange",
  completed: "blue",
  aborted: "red",
  deleted: "gray",
  free_completed: "green",
  free_completed_late: "orange",
  free_missed: "red",
};

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);

  useEffect(() => {
    api.get<DashboardData>("/dashboard").then(setData).catch(() => {});
  }, []);

  if (!data) return <Typography.Text>加载中...</Typography.Text>;

  const statusRows = Object.entries(data.torrents.by_status).map(([status, count]) => ({
    status,
    count,
  }));
  const sourceRows = Object.entries(data.torrents.by_source).map(([source, count]) => ({
    source,
    count,
  }));

  return (
    <div>
      <Typography.Title level={4}>总览仪表盘</Typography.Title>
      <Row gutter={16}>
        <Col span={6}>
          <Card>
            <Statistic title="站点（在线 / 总数）" value={data.sites.enabled} suffix={`/ ${data.sites.total}`} />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic title="在途免费种" value={data.free_seed.in_flight} />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic title="任务总数（按状态）" value={statusRows.reduce((s, r) => s + r.count, 0)} />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic title="任务总数（按来源）" value={sourceRows.reduce((s, r) => s + r.count, 0)} />
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col span={12}>
          <Card title="任务状态分布">
            <Table
              dataSource={statusRows}
              rowKey="status"
              pagination={false}
              columns={[
                { title: "状态", dataIndex: "status", render: (v: string) => <Tag color={statusColor[v] || "default"}>{v}</Tag> },
                { title: "数量", dataIndex: "count" },
              ]}
            />
          </Card>
        </Col>
        <Col span={12}>
          <Card title="任务来源分布">
            <Table
              dataSource={sourceRows}
              rowKey="source"
              pagination={false}
              columns={[
                { title: "来源", dataIndex: "source" },
                { title: "数量", dataIndex: "count" },
              ]}
            />
          </Card>
        </Col>
      </Row>
    </div>
  );
}
