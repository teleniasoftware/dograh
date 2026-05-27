import { cookies } from 'next/headers';
import { NextResponse } from 'next/server';

import { shouldUseSecureAuthCookies } from '@/lib/auth/cookies';

const OSS_TOKEN_COOKIE = 'dograh_auth_token';
const OSS_USER_COOKIE = 'dograh_auth_user';

export async function POST() {
  const cookieStore = await cookies();
  const secure = shouldUseSecureAuthCookies();

  cookieStore.set(OSS_TOKEN_COOKIE, '', {
    httpOnly: true,
    secure,
    sameSite: 'lax',
    maxAge: 0,
    path: '/',
  });

  cookieStore.set(OSS_USER_COOKIE, '', {
    httpOnly: true,
    secure,
    sameSite: 'lax',
    maxAge: 0,
    path: '/',
  });

  return NextResponse.json({ success: true });
}
