/**
 * cron 表达式的构建与解析（前端）。
 *
 * **为什么自己写**：没有现成的、适配 Vue3 + Ant Design Vue 的 cron 可视化
 * 构建器（npm 上的 Vue cron 组件都是 2022 年的、基于 Element 系，风格不统一）。
 * 故自建构建器，UI 用 Ant Design Vue 原生组件。
 *
 * **cronstrue** 只用来生成**人类可读描述**（如「每隔 10 分钟」）——
 * 表达式解析与下次运行时间计算自己实现（前端算，零请求）。
 *
 * 5 字段格式：``分 时 日 月 星期``
 */

import cronstrue from 'cronstrue/i18n';
import dayjs from 'dayjs';

// ==================================================================== 字段选项

/** 分钟字段的预设选项。 */
export const MINUTE_PRESETS: { value: string; label: string }[] = [
  { value: '*', label: '每分钟' },
  { value: '*/5', label: '每 5 分钟' },
  { value: '*/10', label: '每 10 分钟' },
  { value: '*/15', label: '每 15 分钟' },
  { value: '*/30', label: '每 30 分钟' },
  { value: '0', label: '整点（第 0 分钟）' },
];

/** 小时字段的预设选项。 */
export const HOUR_PRESETS: { value: string; label: string }[] = [
  { value: '*', label: '每小时' },
  { value: '*/2', label: '每 2 小时' },
  { value: '*/3', label: '每 3 小时' },
  { value: '*/4', label: '每 4 小时' },
  { value: '*/6', label: '每 6 小时' },
  { value: '*/12', label: '每 12 小时' },
];

/** 日字段的预设选项。 */
export const DAY_PRESETS: { value: string; label: string }[] = [
  { value: '*', label: '每天' },
  { value: '*/2', label: '每 2 天' },
  { value: '*/7', label: '每 7 天' },
  { value: '1', label: '每月 1 号' },
  { value: '15', label: '每月 15 号' },
];

/** 月字段的预设选项。 */
export const MONTH_PRESETS: { value: string; label: string }[] = [
  { value: '*', label: '每月' },
  { value: '*/3', label: '每 3 个月' },
  { value: '*/6', label: '每 6 个月' },
  { value: '1', label: '1 月' },
  { value: '7', label: '7 月' },
];

/** 星期字段的预设选项（0 = 周日）。 */
export const WEEKDAY_PRESETS: { value: string; label: string }[] = [
  { value: '*', label: '每天' },
  { value: '1-5', label: '周一至周五' },
  { value: '0-6', label: '每周（周一至周日）' },
  { value: '0', label: '每周日' },
  { value: '6', label: '每周六' },
];

// ==================================================================== 类型

/** cron 的 5 个字段（模块私有——外部只见 :type:`CronFieldsState`）。 */
interface CronFields {
  minute: string;
  hour: string;
  day: string;
  month: string;
  weekday: string;
}

/** 单个字段的编辑状态：预设值或自定义串。 */
export interface CronFieldState {
  /** 选中的预设值；空串表示用自定义。 */
  preset: string;
  /** 自定义表达式（preset 为空时生效）。 */
  custom: string;
}

/** 5 个字段的编辑状态。 */
export type CronFieldsState = Record<keyof CronFields, CronFieldState>;

// ==================================================================== 表达式 ↔ 字段

/**
 * 把 cron 表达式拆成 5 个字段（不合法时返回 null）。
 *
 * **模块私有**——外部只用 :func:`cronToState` / :func:`stateToCron`，
 * 字段级的中间表示不作为公开契约。
 */
function parseCron(expr: string): CronFields | null {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minute, hour, day, month, weekday] = parts;
  // 上面已断言长度是 5，这里的 undefined 判定只为让类型收窄成立
  //（noUncheckedIndexedAccess 下数组下标取值的类型含 undefined）。
  if (
    minute === undefined ||
    hour === undefined ||
    day === undefined ||
    month === undefined ||
    weekday === undefined
  ) {
    return null;
  }
  return { minute, hour, day, month, weekday };
}

/** 把 5 个字段拼成 cron 表达式（模块私有）。 */
function formatCron(fields: CronFields): string {
  return [fields.minute, fields.hour, fields.day, fields.month, fields.weekday].join(' ');
}

/**
 * 把 cron 表达式转成编辑状态（预设匹配不上就落「自定义」）。
 *
 * 预设里有的用预设（用户能看懂选项），没有的用自定义（仍可编辑）。
 */
export function cronToState(expr: string): CronFieldsState {
  const fields = parseCron(expr);
  const empty: CronFieldsState = {
    minute: { preset: '', custom: '*' },
    hour: { preset: '', custom: '*' },
    day: { preset: '', custom: '*' },
    month: { preset: '', custom: '*' },
    weekday: { preset: '', custom: '*' },
  };
  if (!fields) return empty;

  const pick = (value: string, presets: { value: string }[]): CronFieldState => {
    const hit = presets.find((p) => p.value === value);
    return hit ? { preset: hit.value, custom: value } : { preset: '', custom: value };
  };

  return {
    minute: pick(fields.minute, MINUTE_PRESETS),
    hour: pick(fields.hour, HOUR_PRESETS),
    day: pick(fields.day, DAY_PRESETS),
    month: pick(fields.month, MONTH_PRESETS),
    weekday: pick(fields.weekday, WEEKDAY_PRESETS),
  };
}

/** 把编辑状态转回 cron 表达式（自定义优先——preset 为空时用它）。 */
export function stateToCron(state: CronFieldsState): string {
  const value = (s: CronFieldState): string => (s.preset === '' ? s.custom.trim() : s.preset);
  return formatCron({
    minute: value(state.minute),
    hour: value(state.hour),
    day: value(state.day),
    month: value(state.month),
    weekday: value(state.weekday),
  });
}

// ==================================================================== 描述与预览

/** cron 表达式 → 人类可读描述（中文）。不合法时返回空串。 */
export function describeCron(expr: string): string {
  try {
    return cronstrue.toString(expr, { locale: 'zh_CN' });
  } catch {
    return '';
  }
}

/** 数字类字段（分/时/月）允许的字符。 */
const _NUMERIC_FIELD_RE = /^[\d,\-*/]+$/;
/** 日字段额外允许 `?` `L` `W`。 */
const _DAY_FIELD_RE = /^[\d,\-*/?LW]+$/i;
/** 星期字段额外允许 `?` `L` `#`。 */
const _WEEKDAY_FIELD_RE = /^[\d,\-*/?L#]+$/i;

/**
 * cron 表达式是否**可能**合法。
 *
 * 只做**结构性**检查（字段数 + 每字段的字符集）——完整语义校验交给后端
 * （APScheduler 的 ``CronTrigger.from_crontab``）。前端只提示「看起来不对」，
 * 不做权威判断，避免两套解析器行为不一致。
 */
export function isCronShapeValid(expr: string): boolean {
  const fields = parseCron(expr);
  if (!fields) return false;
  return (
    _NUMERIC_FIELD_RE.test(fields.minute) &&
    _NUMERIC_FIELD_RE.test(fields.hour) &&
    _DAY_FIELD_RE.test(fields.day) &&
    _NUMERIC_FIELD_RE.test(fields.month) &&
    _WEEKDAY_FIELD_RE.test(fields.weekday)
  );
}

// ---- 下次运行时间计算 ----

/** 单个字段的匹配器（支持通配、单值、范围、列表、步长）。 */
function fieldMatches(spec: string, value: number): boolean {
  if (spec === '*' || spec === '?') return true;
  for (const part of spec.split(',')) {
    const seg = part.trim();
    if (!seg) continue;
    // 步长：*/n 或 a-b/n
    const [rangePart, stepPart] = seg.split('/');
    const step = stepPart ? Number(stepPart) : 1;
    if (!Number.isFinite(step) || step <= 0) continue;
    let lo: number;
    let hi: number;
    // seg 非空 → split('/') 至少产出一个元素，rangePart 必然存在
    const range = rangePart ?? seg;
    if (range === '*' || range === '?') {
      lo = 0;
      hi = Number.MAX_SAFE_INTEGER;
    } else if (range.includes('-')) {
      const [a, b] = range.split('-').map((n) => Number(n));
      // 含 '-' → split 至少产出两个元素；判定只为让类型收窄成立
      if (a === undefined || b === undefined) continue;
      lo = a;
      hi = b;
    } else {
      lo = Number(range);
      hi = step > 1 ? lo + step * 10 : lo;
    }
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) continue;
    if (value >= lo && value <= hi && (value - lo) % step === 0) return true;
  }
  return false;
}

/**
 * 计算 cron 表达式的**未来 N 次**运行时间（模块私有）。
 *
 * **纯前端计算**（零请求）。逐分钟向前搜索——最多看 366 天（分钟级
 * 表达式下足够；cron 最短周期是 1 分钟）。
 *
 * 星期字段的语义：cron 里 0 和 7 都是周日，**与日期字段是 OR 关系**
 * （任一匹配即触发）——本函数按此实现。
 */
function nextRunTimes(expr: string, count = 3): Date[] {
  const fields = parseCron(expr);
  if (!fields) return [];
  const result: Date[] = [];
  // 从下一分钟开始找（当前分钟的整点若恰好匹配，也算一次——
  // 但 cron 是「到点触发」，已开始的那次不该重复算，故从下一分钟起）
  let cursor = dayjs().add(1, 'minute').startOf('minute');
  const limit = 366 * 24 * 60;

  for (let i = 0; i < limit && result.length < count; i++) {
    const d = cursor;
    const dateOk = fieldMatches(fields.day, d.date());
    const weekdayOk = fieldMatches(fields.weekday, d.day());
    // 日与星期是 OR 关系（cron 标准语义）
    const dayOk = dateOk || weekdayOk;
    if (
      fieldMatches(fields.minute, d.minute()) &&
      fieldMatches(fields.hour, d.hour()) &&
      dayOk &&
      fieldMatches(fields.month, d.month() + 1)
    ) {
      result.push(d.toDate());
    }
    cursor = cursor.add(1, 'minute');
  }
  return result;
}

/** 下次运行时间的展示文案（相对现在 + 绝对时间）。 */
export function formatNextRuns(expr: string, count = 3): string[] {
  return nextRunTimes(expr, count).map((d) => {
    const text = dayjs(d).format('MM-DD HH:mm');
    const diffMin = Math.round((d.getTime() - Date.now()) / 60000);
    let rel: string;
    if (diffMin < 60) rel = `${String(diffMin)} 分钟后`;
    else if (diffMin < 60 * 24) rel = `${String(Math.round(diffMin / 60))} 小时后`;
    else rel = `${String(Math.round(diffMin / 1440))} 天后`;
    return `${text}（${rel}）`;
  });
}
