// ==================== COMMON FUNCTIONS ====================

// Check login status
async function checkLogin() {
    const artistId = localStorage.getItem('artist_id');
    if (!artistId) {
        window.location.href = 'login.html';
        return false;
    }
    
    try {
        const response = await fetch('/api/check_session');
        const data = await response.json();
        if (!data.logged_in) {
            localStorage.clear();
            window.location.href = 'login.html';
            return false;
        }
        return true;
    } catch (error) {
        localStorage.clear();
        window.location.href = 'login.html';
        return false;
    }
}

// Initialize sidebar toggle
function initSidebar() {
    const sidebar = document.getElementById('sidebar');
    const menuBtn = document.getElementById('menuBtn');
    
    if (menuBtn && sidebar) {
        menuBtn.onclick = () => {
            sidebar.classList.toggle('collapsed');
            localStorage.setItem('sidebarCollapsed', sidebar.classList.contains('collapsed'));
        };
        
        // Load saved state
        if (localStorage.getItem('sidebarCollapsed') === 'true') {
            sidebar.classList.add('collapsed');
        }
    }
}

// Set active menu item
function setActiveMenu() {
    const currentPage = window.location.pathname.split('/').pop();
    const menuItems = document.querySelectorAll('.menu-item');
    
    menuItems.forEach(item => {
        item.classList.remove('active');
        const link = item.getAttribute('href') || item.querySelector('a')?.getAttribute('href');
        if (link && link.includes(currentPage)) {
            item.classList.add('active');
        }
    });
}

// Load artist profile in topnav
async function loadArtistProfile() {
    try {
        const artistId = localStorage.getItem('artist_id');
        if (!artistId) return;
        
        const response = await fetch(`/api/profile`);
        if (response.ok) {
            const data = await response.json();
            const artist = data.artist;
            
            // Update profile pic
            const profilePics = document.querySelectorAll('.profile-pic, .sidebar-logo');
            profilePics.forEach(pic => {
                if (artist.Portfolio_Path && artist.Portfolio_Path !== '/portfolio/john_doe') {
                    pic.innerHTML = `<img src="${artist.Portfolio_Path}" alt="Profile">`;
                } else {
                    const initials = (artist.First_Name[0] || '') + (artist.Last_Name[0] || '');
                    pic.innerHTML = `<div style="background: #4A6CF7; color: white; display: flex; align-items: center; justify-content: center; width: 100%; height: 100%; font-weight: bold; border-radius: 50%;">${initials}</div>`;
                }
            });
            
            // Update artist name in sidebar if exists
            const artistNameElement = document.getElementById('artistName');
            if (artistNameElement) {
                artistNameElement.textContent = `${artist.First_Name} ${artist.Last_Name}`;
            }
        }
    } catch (error) {
        console.error('Error loading profile:', error);
    }
}

// Logout function
function setupLogout() {
    const logoutButtons = document.querySelectorAll('[data-logout]');
    logoutButtons.forEach(btn => {
        btn.addEventListener('click', async function(e) {
            e.preventDefault();
            
            try {
                await fetch('/api/logout', { method: 'POST' });
            } catch (error) {
                // Continue even if API fails
            }
            
            localStorage.clear();
            window.location.href = 'login.html';
        });
    });
}

// Format date
function formatDate(dateString) {
    if (!dateString) return 'N/A';
    const date = new Date(dateString);
    return date.toLocaleDateString('en-IN', {
        day: '2-digit',
        month: 'short',
        year: 'numeric'
    });
}

// Format currency
function formatCurrency(amount) {
    if (!amount) return '₹0';
    return '₹' + parseFloat(amount).toFixed(2);
}

// Initialize common functionality
async function initCommon() {
    await checkLogin();
    initSidebar();
    setActiveMenu();
    loadArtistProfile();
    setupLogout();
}

// Run on page load
document.addEventListener('DOMContentLoaded', initCommon);