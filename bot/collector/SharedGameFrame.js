/** Resolve the collector-owned Aviator iframe without waiting for history payouts. */
export async function resolveSharedGameFrame(frameManager, page, { timeoutMs, signal } = {}) {
  const pageManager = frameManager?.pageManager;
  if (pageManager?.getAviatorFrame) {
    const sharedPage = await pageManager.getAviatorPage();
    if (sharedPage !== page) {
      throw Object.assign(new Error('COLLECTOR_PAGE_IS_NOT_SHARED_AVIATOR_PAGE'), {
        code: 'COLLECTOR_PAGE_IS_NOT_SHARED_AVIATOR_PAGE',
      });
    }
    const frame = await pageManager.getAviatorFrame();
    if (!frame || frame.isDetached?.()) {
      throw Object.assign(new Error('SHARED_GAME_FRAME_NOT_READY'), {
        code: 'SHARED_GAME_FRAME_NOT_READY',
      });
    }
    return frame;
  }

  const frame = await frameManager.waitForFrame(page, { timeoutMs, signal });
  if (!frame || frame.isDetached?.()) {
    throw Object.assign(new Error('AVIATOR_FRAME_DETACHED'), { code: 'AVIATOR_FRAME_DETACHED' });
  }
  return frame;
}
