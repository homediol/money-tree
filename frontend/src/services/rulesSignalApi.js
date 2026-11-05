export async function copyTextToClipboard(value, clipboard = globalThis.navigator?.clipboard) {
  if (!clipboard || typeof clipboard.writeText !== 'function') {
    throw new Error('Clipboard access is unavailable.');
  }
  await clipboard.writeText(String(value));
  return true;
}
