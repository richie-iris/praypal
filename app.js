/**
 * PrayPal — Minimalist Sanctuary Companion
 * Clean theme management and lightweight interactions
 */

(function () {
  'use strict';

  // Theme Management (Light / Dark)
  const themeToggle = document.getElementById('themeToggle');
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)');

  function getSavedTheme() {
    const saved = localStorage.getItem('praypal_theme');
    if (saved) return saved;
    return prefersDark.matches ? 'dark' : 'light';
  }

  function applyTheme(theme) {
    if (theme === 'dark') {
      document.documentElement.setAttribute('data-theme', 'dark');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    localStorage.setItem('praypal_theme', theme);
  }

  // Initialize Theme
  applyTheme(getSavedTheme());

  if (themeToggle) {
    themeToggle.addEventListener('click', function () {
      const current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
      applyTheme(current === 'dark' ? 'light' : 'dark');
    });
  }

  // Listen for system appearance change
  prefersDark.addEventListener('change', function (e) {
    if (!localStorage.getItem('praypal_theme')) {
      applyTheme(e.matches ? 'dark' : 'light');
    }
  });

  // Soft vibration on call/text clicks on mobile devices (if supported)
  const actionButtons = document.querySelectorAll('.btn-primary, .btn-secondary, .nav-call-btn');
  actionButtons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (typeof navigator !== 'undefined' && navigator.vibrate) {
        navigator.vibrate(10);
      }
    });
  });

})();
