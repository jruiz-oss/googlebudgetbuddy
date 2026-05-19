/**
 * Logo — renders the BudgetBuddy Google logo SVG.
 *
 * Props:
 *   size  {number}  width & height in px (default 40)
 *   style {object}  extra inline styles
 */
export default function Logo({ size = 40, style = {} }) {
  return (
    <img
      src="/logo.svg"
      alt="BudgetBuddy"
      width={size}
      height={size}
      style={{ display: 'block', flexShrink: 0, ...style }}
    />
  );
}
