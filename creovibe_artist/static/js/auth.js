/**
 * auth.js — Artist Module (CreoVibe)
 * Handles session verification, login, logout, and back-button protection.
 */

class AuthManager {
    constructor() {
        this.artistId = null;
        this.username = null;
        this.init();
    }

    init() {
        // No cookie-based token for artist module — session-only via Flask
        // Nothing to read from cookies here
    }

    // -------------------------------------------------------
    // CHECK SESSION — calls /api/auth/verify (artist-specific)
    // -------------------------------------------------------
    async checkAuth() {
        try {
            const response = await fetch('/api/auth/verify', {
                credentials: 'include'
            });

            if (response.ok) {
                const data = await response.json();
                if (data.authenticated) {
                    this.artistId = data.artist_id;
                    this.username = data.username;
                    return true;
                }
            }
            return false;
        } catch (error) {
            console.error('Auth check failed:', error);
            return false;
        }
    }

    // -------------------------------------------------------
    // LOGIN — POST to /api/login
    // -------------------------------------------------------
    async login(username, password) {
        try {
            const response = await fetch('/api/login', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                credentials: 'include',
                body: JSON.stringify({ username, password })
            });

            const data = await response.json();

            if (data.success) {
                this.artistId = data.artist_id;
                this.username = username;
                return { success: true, data };
            } else {
                return { success: false, error: data.error };
            }
        } catch (error) {
            return { success: false, error: 'Login failed. Please try again.' };
        }
    }

    // -------------------------------------------------------
    // LOGOUT — POST to /api/logout, then hard redirect to /login
    // Uses window.location.replace() so back button cannot return
    // -------------------------------------------------------
    logout() {
        fetch('/api/logout', {
            method: 'POST',
            credentials: 'include'
        })
        .then(() => {
            // replace() removes current page from history stack
            window.location.replace('/login');
        })
        .catch(() => {
            // Even if fetch fails, still redirect
            window.location.replace('/login');
        });
    }

    // -------------------------------------------------------
    // AUTH FETCH — auto-redirects on 401
    // -------------------------------------------------------
    async authFetch(url, options = {}) {
        const defaultOptions = {
            credentials: 'include',
            headers: {
                'Content-Type': 'application/json'
            }
        };

        try {
            const response = await fetch(url, { ...defaultOptions, ...options });

            if (response.status === 401) {
                window.location.replace('/login');
                return null;
            }

            return response;
        } catch (error) {
            console.error('authFetch error:', error);
            return null;
        }
    }
}

// -------------------------------------------------------
// Global instance
// -------------------------------------------------------
window.authManager = new AuthManager();

// -------------------------------------------------------
// PAGE LOAD PROTECTION
// Runs on every page except /login
// Uses replace() so back button cannot bypass auth
// -------------------------------------------------------
document.addEventListener('DOMContentLoaded', async function () {
    const path = window.location.pathname;

    // Skip check on login page itself
    if (path === '/login' || path === '/login.html') return;

    const isAuthenticated = await authManager.checkAuth();

    if (!isAuthenticated) {
        window.location.replace('/login');
    }
});

// -------------------------------------------------------
// BACK BUTTON PROTECTION (pageshow event)
// Fires when user navigates back to a cached page.
// Re-verifies session with the server on every page restore.
// -------------------------------------------------------
window.addEventListener('pageshow', function (event) {
    const path = window.location.pathname;

    // Skip check on login page
    if (path === '/login' || path === '/login.html') return;

    // event.persisted = true means page was loaded from bfcache (back button)
    // performance.navigation.type === 2 is the legacy equivalent
    const isBackNavigation = event.persisted ||
        (window.performance && window.performance.navigation.type === 2);

    if (isBackNavigation) {
        fetch('/api/auth/verify', { credentials: 'include' })
            .then(res => res.json())
            .then(data => {
                if (!data.authenticated) {
                    window.location.replace('/login');
                }
            })
            .catch(() => {
                // If the request fails, assume logged out for safety
                window.location.replace('/login');
            });
    }
});
