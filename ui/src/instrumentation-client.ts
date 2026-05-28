// This file configures the initialization of Sentry on the client.
// The added config here will be used whenever a users loads a page in their browser.
// https://docs.sentry.io/platforms/javascript/guides/nextjs/

import * as Sentry from "@sentry/nextjs";
import posthog from "posthog-js";

// Drop errors originating from browser extensions (MetaMask's inpage.js,
// injected widgets, etc.) by matching their URL scheme.
const sharedSentryOptions = {
  debug: false,
  denyUrls: [
    /^chrome-extension:\/\//i,
    /^moz-extension:\/\//i,
    /^safari-extension:\/\//i,
    /^safari-web-extension:\/\//i,
  ],
};

// Initialize Sentry - prioritize NEXT_PUBLIC env vars, fallback to API
const initSentry = () => {
  const hasPublicConfig = process.env.NEXT_PUBLIC_SENTRY_DSN;


  if (hasPublicConfig) {
    // Use client-side environment variables
    Sentry.init({
      dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
      ...sharedSentryOptions,
    });
    console.log('Sentry initialized from NEXT_PUBLIC config');
  } else {
    // Fallback to API-based configuration
    fetch('/api/config/sentry')
      .then(res => res.json())
      .then(config => {
        if (config.enabled && config.dsn) {
          Sentry.init({
            dsn: config.dsn,
            ...sharedSentryOptions,
          });
          console.log('Sentry initialized from API config');
        } else {
          console.log('Sentry disabled (not enabled or DSN not configured)');
        }
      })
      .catch(err => {
        console.error('Failed to fetch Sentry configuration:', err);
      });
  }
};

if (process.env.NEXT_PUBLIC_NODE_ENV !== 'development') {
  initSentry();
}

// Initialize PostHog - prioritize NEXT_PUBLIC env vars, fallback to API
const initPostHog = () => {
  const hasPublicConfig = process.env.NEXT_PUBLIC_POSTHOG_KEY;


  if (hasPublicConfig) {
    const apiHost = process.env.NEXT_PUBLIC_POSTHOG_HOST;
    if (!apiHost) {
      console.log('PostHog disabled (NEXT_PUBLIC_POSTHOG_HOST not configured)');
      return;
    }

    // Use client-side environment variables
    posthog.init(process.env.NEXT_PUBLIC_POSTHOG_KEY!, {
      api_host: apiHost,
      ui_host: process.env.NEXT_PUBLIC_POSTHOG_UI_HOST,
      capture_pageview: 'history_change',
      capture_pageleave: true,
      capture_exceptions: true,
      cross_subdomain_cookie: true,
      debug: process.env.NEXT_PUBLIC_NODE_ENV === 'development',
    });
    console.log('PostHog initialized from NEXT_PUBLIC config');
  } else {
    // Fallback to API-based configuration
    fetch('/api/config/posthog')
      .then(res => res.json())
      .then(config => {
        if (config.enabled && config.key && config.host) {
          posthog.init(config.key, {
            api_host: config.host,
            ui_host: config.uiHost,
            capture_pageview: 'history_change',
            capture_pageleave: true,
            capture_exceptions: true,
            cross_subdomain_cookie: true,
            debug: process.env.NEXT_PUBLIC_NODE_ENV === 'development',
          });
          console.log('PostHog initialized from API config');
        } else {
          console.log('PostHog disabled (not enabled or not configured)');
        }
      })
      .catch(err => {
        console.error('Failed to fetch PostHog configuration:', err);
      });
  }
};

if (process.env.NEXT_PUBLIC_NODE_ENV !== 'development') {
  initPostHog();
}


export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
