export function pct(value, digits = 1) {
  return typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(digits)}%` : 'N/A';
}

export function mult(value) {
  return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(2)}x` : 'N/A';
}

