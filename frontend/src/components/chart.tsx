"use client";

import dynamic from "next/dynamic";
import { useTheme } from "@/lib/auth";
import type { EChartsOption } from "echarts";

const ReactECharts = dynamic(() => import("echarts-for-react"), { ssr: false });

export type ChartSpec = { type: "bar" | "line" | "pie" | "area"; title?: string; x: (string | number)[]; series: { name: string; data: (number | null)[] }[]; format?: string | null; unit?: string | null; query_ref?: string };

const PALETTE = ["#2563eb", "#7c3aed", "#059669", "#d97706", "#dc2626", "#0891b2", "#db2777", "#65a30d"];

export function Chart({ spec, height = 260 }: { spec: ChartSpec; height?: number }) {
  const { dark } = useTheme();
  const text = dark ? "#93a0bd" : "#64748b";
  const grid = dark ? "#243052" : "#e4e7ec";
  const fmt = (v: number) => (spec.format === "percent" ? `${v.toFixed(1)}%` : Math.abs(v) >= 1e7 ? `${(v / 1e7).toFixed(1)}Cr` : Math.abs(v) >= 1e5 ? `${(v / 1e5).toFixed(1)}L` : Math.abs(v) >= 1e3 ? `${(v / 1e3).toFixed(0)}K` : `${v}`);
  let option: EChartsOption;
  if (spec.type === "pie") {
    option = { color: PALETTE, tooltip: { trigger: "item" }, series: [{ type: "pie", radius: ["40%", "70%"], data: spec.x.map((x, i) => ({ name: String(x), value: spec.series[0]?.data[i] ?? 0 })), label: { color: text } }] };
  } else {
    option = {
      color: PALETTE,
      tooltip: { trigger: "axis", valueFormatter: (v) => (typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(v)) },
      legend: spec.series.length > 1 ? { top: 0, textStyle: { color: text, fontSize: 11 } } : undefined,
      grid: { left: 8, right: 12, top: spec.series.length > 1 ? 30 : 12, bottom: 8, containLabel: true },
      xAxis: { type: "category", data: spec.x.map(String), axisLabel: { color: text, fontSize: 11, rotate: spec.x.length > 8 ? 30 : 0 }, axisLine: { lineStyle: { color: grid } } },
      yAxis: { type: "value", axisLabel: { color: text, fontSize: 11, formatter: fmt }, splitLine: { lineStyle: { color: grid } } },
      series: spec.series.map((s) =>
        spec.type === "bar"
          ? ({ name: s.name, type: "bar" as const, data: s.data, barMaxWidth: 36, itemStyle: { borderRadius: [4, 4, 0, 0] } })
          : ({ name: s.name, type: "line" as const, data: s.data, smooth: true, areaStyle: spec.type === "area" ? { opacity: 0.15 } : undefined }),
      ),
    };
  }
  return (
    <div className="rounded-lg border bg-surface p-3">
      {spec.title && <p className="mb-1 text-xs font-medium text-muted">{spec.title}</p>}
      <ReactECharts option={option} style={{ height }} notMerge lazyUpdate theme={undefined} />
    </div>
  );
}
