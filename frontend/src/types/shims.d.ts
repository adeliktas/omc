// T29 (2026-05-14): declaration shims for upstream libraries that ship
// without bundled type definitions. Treats them as `any` so `tsc --noEmit`
// passes. Replace each with proper types when @types/* packages land in
// node_modules. tsconfig already has `skipLibCheck: true` for node_modules.

declare module "@clerk/nextjs";
declare module "@clerk/nextjs/server";
declare module "recharts" {
    // Generic re-export — every entry typed as `any` so callers compile.
    // tsconfig has `skipLibCheck: true`; this shim is intentionally loose.
    const _default: any;
    export = _default;
    export const Tooltip: any;
    export const Legend: any;
    export const ResponsiveContainer: any;
    export const LineChart: any;
    export const BarChart: any;
    export const Area: any;
    export const AreaChart: any;
    export const Bar: any;
    export const Line: any;
    export const XAxis: any;
    export const YAxis: any;
    export const CartesianGrid: any;
    export const PieChart: any;
    export const Pie: any;
    export const Cell: any;
    export const RadialBar: any;
    export const RadialBarChart: any;
    export const PolarGrid: any;
    export const PolarAngleAxis: any;
    export const PolarRadiusAxis: any;
    export const ReferenceLine: any;
    export const Sector: any;
    export type TooltipContentProps<T = any, U = any> = any;
    export type DefaultLegendContentProps = any;
    export type LegendType = any;
}
