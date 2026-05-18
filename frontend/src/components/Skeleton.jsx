/**
 * Lightweight skeleton loader primitives.
 *
 * <Skeleton width={...} height={...} />          – generic block
 * <SkeletonText lines={3} />                     – stacked text lines
 * <SkeletonStatTile />                           – matches .bb-stat
 * <SkeletonCard height={...}>                    – matches .bb-card
 * <SkeletonTable rows={5} cols={6} />            – matches .bb-table
 *
 * All use the .bb-skeleton class which animates a shimmer in index.css.
 */
import React from 'react';

export function Skeleton({ width = '100%', height = 12, radius = 6, style = {} }) {
  return (
    <div
      className="bb-skeleton"
      style={{ width, height, borderRadius: radius, ...style }}
      aria-hidden="true"
    />
  );
}

export function SkeletonText({ lines = 1, lastWidth = '60%' }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }} aria-hidden="true">
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton
          key={i}
          width={i === lines - 1 ? lastWidth : '100%'}
          height={10}
        />
      ))}
    </div>
  );
}

export function SkeletonStatTile() {
  return (
    <div className="bb-stat" aria-busy="true">
      <Skeleton width="50%" height={9} />
      <Skeleton width="40%" height={28} style={{ marginTop: 6 }} />
      <Skeleton width="65%" height={10} style={{ marginTop: 6 }} />
    </div>
  );
}

export function SkeletonCard({ height = 200, padding = 18 }) {
  return (
    <div className="bb-card" style={{ padding }} aria-busy="true">
      <Skeleton width="35%" height={14} />
      <Skeleton width="100%" height={height - 60} style={{ marginTop: 12 }} />
    </div>
  );
}

export function SkeletonTable({ rows = 5, cols = 6 }) {
  return (
    <div className="bb-card" aria-busy="true">
      <div className="bb-section" style={{ paddingBottom: 4 }}>
        <Skeleton width="30%" height={14} />
      </div>
      <table className="bb-table">
        <thead>
          <tr>
            {Array.from({ length: cols }).map((_, c) => (
              <th key={c}><Skeleton width="60%" height={9} /></th>
            ))}
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: rows }).map((_, r) => (
            <tr key={r}>
              {Array.from({ length: cols }).map((_, c) => (
                <td key={c}>
                  <Skeleton width={c === 0 ? '70%' : '50%'} height={11} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Skeleton that mirrors the per-account block on the Home page. */
export function SkeletonAccountBlock() {
  return (
    <div className="bb-card" style={{ marginBottom: 20 }} aria-busy="true">
      <div
        className="bb-row-between"
        style={{
          padding: '12px 20px',
          background: '#f0f2f4',
          borderRadius: '10px 10px 0 0',
          borderBottom: '1px solid #e2e5e8',
        }}
      >
        <div style={{ flex: 1 }}>
          <Skeleton width={180} height={14} />
          <Skeleton width={140} height={10} style={{ marginTop: 6 }} />
        </div>
        <Skeleton width={110} height={32} radius={8} />
      </div>
      <table className="bb-table">
        <thead>
          <tr>
            {Array.from({ length: 9 }).map((_, c) => (
              <th key={c}><Skeleton width="60%" height={9} /></th>
            ))}
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: 4 }).map((_, r) => (
            <tr key={r}>
              {Array.from({ length: 9 }).map((_, c) => (
                <td key={c}>
                  <Skeleton width={c === 0 ? '75%' : '55%'} height={11} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
