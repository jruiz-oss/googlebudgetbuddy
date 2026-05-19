import { useState, useMemo } from 'react';
import { Check, Settings } from 'lucide-react';

function campaignKey(c) {
  const digits = String(c.google_campaign_id || '').replace(/\D/g, '');
  return digits || `db:${c.id}`;
}

// Generate notifications from real account pacing data
function buildNotifications(accounts) {
  const today = new Date();
  // Spend data from Google Ads is through EOD of the prior day.
  const daysIn = Math.max(today.getDate() - 1, 1);
  const daysInMonth = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
  const THRESHOLD = 5;
  const noti = [];

  for (const account of accounts) {
    const campaigns = account.campaigns || [];
    const segBudgets = {};
    for (const c of campaigns) {
      const label = c.budget_label || 'Primary';
      segBudgets[label] = Math.max(segBudgets[label] || 0, c.monthly_budget || 0);
    }
    const monthly = Object.values(segBudgets).reduce((s, b) => s + b, 0);
    const mostRecentDate = campaigns.reduce((latest, c) => {
      const d = c.latest_pacing?.date;
      if (!d) return latest;
      return !latest || d > latest ? d : latest;
    }, null);
    // Deduplicate spend by google_campaign_id and only use the latest pacing run.
    const _nSeenGids = new Set();
    const spend = typeof account.mtd_spend === 'number'
      ? account.mtd_spend
      : campaigns.reduce((s, c) => {
        if (mostRecentDate && c.latest_pacing?.date !== mostRecentDate) return s;
        const key = campaignKey(c);
        if (_nSeenGids.has(key)) return s;
        _nSeenGids.add(key);
        return s + (c.latest_pacing?.actual_spend || 0);
      }, 0);
    if (monthly === 0 && spend === 0) continue;

    const idealSpend = monthly > 0 ? monthly * (daysIn / daysInMonth) : 0;
    const deltaPct   = idealSpend > 0 ? ((spend / idealSpend) - 1) * 100 : 0;
    const dailyRec   = daysInMonth > 0 ? Math.max(0, monthly - spend) / daysInMonth : 0;

    const fmt = (n) => '$' + Math.round(n || 0).toLocaleString('en-US');
    const fmtPct = (n) => Math.abs(n).toFixed(1) + '%';

    if (deltaPct > THRESHOLD) {
      noti.push({
        id: `over-${account.id}`,
        kind: 'over',
        unread: deltaPct > 20,
        title: `${account.account_name} crossed +${THRESHOLD}% pace`,
        desc: `Currently +${fmtPct(deltaPct)} over — MTD ${fmt(spend)} vs ideal ${fmt(idealSpend)}. Daily rec ${fmt(dailyRec)}.`,
        ts: 'this sync',
      });
    } else if (deltaPct < -THRESHOLD) {
      noti.push({
        id: `under-${account.id}`,
        kind: 'under',
        unread: Math.abs(deltaPct) > 15,
        title: `${account.account_name} fell below −${THRESHOLD}%`,
        desc: `Currently ${deltaPct.toFixed(1)}% under pace. Daily rec lifted to ${fmt(dailyRec)} to hit monthly ${fmt(monthly)}.`,
        ts: 'this sync',
      });
    }

    // Check for campaigns that are paused but had recent spend
    for (const c of campaigns) {
      if (c.is_active === false && (c.latest_pacing?.actual_spend || 0) > 0) {
        noti.push({
          id: `paused-${c.id}`,
          kind: 'info',
          unread: false,
          title: 'Campaign paused while spending',
          desc: `${account.account_name} · "${c.campaign_name}" shows spend this month despite being paused.`,
          ts: 'recent',
        });
        break; // one per account
      }
    }
  }

  // Static fallback entries for demo richness
  if (noti.length === 0) {
    noti.push({
      id: 'demo-1',
      kind: 'ok',
      unread: false,
      title: 'Weekly summary ready',
      desc: `${accounts.length} account${accounts.length !== 1 ? 's' : ''} tracked · sync running on schedule.`,
      ts: 'now',
    });
  }

  return noti;
}

const ICON = { over: '!', under: '↓', info: 'i', ok: '✓' };

export default function Notifications({ accounts = [] }) {
  const [filter, setFilter] = useState('all');
  const [readSet, setReadSet] = useState(new Set());

  const allNoti = useMemo(() => buildNotifications(accounts), [accounts]);

  const markAllRead = () => setReadSet(new Set(allNoti.map(n => n.id)));

  const list = allNoti.filter(n => {
    const isUnread = n.unread && !readSet.has(n.id);
    if (filter === 'unread') return isUnread;
    if (filter === 'over')   return n.kind === 'over';
    if (filter === 'under')  return n.kind === 'under';
    return true;
  });

  const unreadCount = allNoti.filter(n => n.unread && !readSet.has(n.id)).length;

  return (
    <div>
      {/* Head */}
      <div className="detail-head" style={{ marginBottom: 16 }}>
        <div>
          <div className="dtitle">Notifications</div>
          <div className="ddesc">{unreadCount} unread · campaign-running alerts, pace flips, and weekly digests</div>
        </div>
        <div className="dactions">
          <button className="btn ghost" onClick={markAllRead}>
            <Check size={13} /> Mark all read
          </button>
          <button className="btn">
            <Settings size={13} /> Notification settings
          </button>
        </div>
      </div>

      {/* Filter bar */}
      <div className="filterbar" style={{ marginBottom: 14 }}>
        <div className="segctrl">
          <button className={filter === 'all'    ? 'active' : ''} onClick={() => setFilter('all')}>All</button>
          <button className={filter === 'unread' ? 'active' : ''} onClick={() => setFilter('unread')}>
            Unread {unreadCount > 0 && <span style={{ opacity: 0.6, marginLeft: 2 }}>{unreadCount}</span>}
          </button>
          <button className={filter === 'over'   ? 'active' : ''} onClick={() => setFilter('over')}>Over pace</button>
          <button className={filter === 'under'  ? 'active' : ''} onClick={() => setFilter('under')}>Under pace</button>
        </div>
      </div>

      {/* List */}
      <div className="noti-list">
        {list.length === 0 ? (
          <div style={{ padding: '40px', textAlign: 'center', color: 'var(--muted)' }}>
            No notifications match this filter
          </div>
        ) : list.map(n => {
          const isUnread = n.unread && !readSet.has(n.id);
          return (
            <div
              key={n.id}
              className={`noti-item ${isUnread ? 'unread' : ''}`}
              onClick={() => setReadSet(s => new Set([...s, n.id]))}
            >
              <div className={`nicon ${n.kind}`}>{ICON[n.kind]}</div>
              <div className="nbody">
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
                  <div className="ntitle">{n.title}</div>
                  <div className="nts">{n.ts}</div>
                </div>
                <div className="ndesc">{n.desc}</div>
              </div>
              {isUnread && <div className="unread-dot" />}
            </div>
          );
        })}
      </div>
    </div>
  );
}
