export function shouldUseSecureAuthCookies(): boolean {
  const configured = process.env.AUTH_COOKIE_SECURE?.toLowerCase();

  if (configured === 'true') {
    return true;
  }

  if (configured === 'false') {
    return false;
  }

  return process.env.NODE_ENV === 'production';
}
