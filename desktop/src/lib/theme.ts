/** Theme choice: system | light | dark. "system" leaves data-theme unset so the CSS follows prefers-color-scheme. */
export type ThemeChoice = 'system' | 'light' | 'dark';
const KEY = 'tal:theme';

export function loadTheme(): ThemeChoice {
  try {
    const v = localStorage.getItem(KEY);
    if (v === 'light' || v === 'dark' || v === 'system') return v;
  } catch {
    /* storage blocked */
  }
  return 'system';
}

export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === 'system') {
    root.removeAttribute('data-theme');
    root.style.removeProperty('color-scheme');
  } else {
    root.setAttribute('data-theme', choice);
    root.style.colorScheme = choice;
  }
  try {
    localStorage.setItem(KEY, choice);
  } catch {
    /* storage blocked: the choice still applies for this page view */
  }
}
