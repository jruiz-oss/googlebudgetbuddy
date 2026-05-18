/**
 * Toast notification system — single grouped panel with per-item status rows.
 *
 * Usage:
 *   const toast = useToast();
 *   toast.success('Saved.');
 *   toast.error('Something broke.');
 *   toast.info('FYI…');
 *   toast.warn('Heads up.');
 *
 * Wrap the app with <ToastProvider> at the root.
 */
import React, { createContext, useCallback, useContext, useState } from 'react';
import { CheckCircle2, AlertCircle, Info, AlertTriangle, X, Bell } from 'lucide-react';

const ToastContext = createContext(null);

let nextId = 1;

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const [panelLeaving, setPanelLeaving] = useState(false);

  const dismiss = useCallback((id) => {
    setToasts((prev) => {
      const next = prev.filter((t) => t.id !== id);
      return next;
    });
  }, []);

  const dismissAll = useCallback(() => {
    setPanelLeaving(true);
    setTimeout(() => {
      setToasts([]);
      setPanelLeaving(false);
    }, 220);
  }, []);

  const push = useCallback(
    (variant, message, opts = {}) => {
      const id = nextId++;
      const ttl = opts.duration ?? 10000;
      setToasts((prev) => [...prev, { id, variant, message, title: opts.title }]);
      if (ttl > 0) {
        setTimeout(() => dismiss(id), ttl);
      }
      return id;
    },
    [dismiss]
  );

  const value = {
    success:    (msg, opts) => push('success', msg, opts),
    error:      (msg, opts) => push('error',   msg, opts),
    info:       (msg, opts) => push('info',    msg, opts),
    warn:       (msg, opts) => push('warn',    msg, opts),
    dismiss,
    dismissAll,
  };

  return (
    <ToastContext.Provider value={value}>
      {children}

      {toasts.length > 0 && (
        <div
          className={`bb-toast-panel${panelLeaving ? ' is-leaving' : ''}`}
          role="region"
          aria-live="polite"
          aria-label="Notifications"
        >
          {/* Panel header */}
          <div className="bb-toast-panel-header">
            <span className="bb-toast-panel-label">
              <Bell size={12} aria-hidden="true" />
              Notifications
            </span>
            <button
              className="bb-toast-panel-close-all"
              onClick={dismissAll}
              aria-label="Dismiss all notifications"
              type="button"
            >
              <X size={13} aria-hidden="true" />
            </button>
          </div>

          {/* Notification rows */}
          <div className="bb-toast-panel-body">
            {toasts.map((t, i) => (
              <ToastRow
                key={t.id}
                toast={t}
                onDismiss={() => dismiss(t.id)}
                showDivider={i < toasts.length - 1}
              />
            ))}
          </div>
        </div>
      )}
    </ToastContext.Provider>
  );
}

/* ── per-row variant config ── */
const VARIANT = {
  success: { Icon: CheckCircle2, color: '#10b981' },
  error:   { Icon: AlertCircle,  color: '#ef4444' },
  warn:    { Icon: AlertTriangle, color: '#f59e0b' },
  info:    { Icon: Info,          color: '#3b82f6' },
};

function ToastRow({ toast, onDismiss, showDivider }) {
  const cfg = VARIANT[toast.variant] || VARIANT.info;
  const { Icon, color } = cfg;

  return (
    <div className={`bb-toast-row${showDivider ? ' bb-toast-row--divider' : ''}`}>
      {/* Status icon */}
      <span className="bb-toast-row-icon" style={{ color }} aria-hidden="true">
        <Icon size={15} strokeWidth={2.2} />
      </span>

      {/* Text */}
      <div className="bb-toast-row-body">
        {toast.title && (
          <div className="bb-toast-row-title">{toast.title}</div>
        )}
        <div className="bb-toast-row-msg">{toast.message}</div>
      </div>

      {/* Per-row dismiss */}
      <button
        className="bb-toast-row-dismiss"
        onClick={onDismiss}
        aria-label="Dismiss this notification"
        type="button"
      >
        <X size={11} aria-hidden="true" />
      </button>
    </div>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    const noop = (m) => console.warn('[toast outside provider]', m);
    return { success: noop, error: noop, info: noop, warn: noop, dismiss: () => {}, dismissAll: () => {} };
  }
  return ctx;
}
