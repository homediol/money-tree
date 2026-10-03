// Authentication and transport availability are separate states. Once access
// has been verified, transport changes must not remove the mounted application.
export function hasUsableApplicationState(access) {
  return access === 'granted';
}

export function accessScreenState(access, connectionState) {
  if (hasUsableApplicationState(access)) return connectionState;
  if (access === 'required') return 'AUTH_REQUIRED';
  if (access === 'error') return 'ACCESS_CHECK_FAILED';
  if (connectionState === 'DISCONNECTED' || connectionState === 'RECONNECTING') return connectionState;
  return 'INITIAL_CONNECT';
}
