/**
 * Furnace Management System - Main JavaScript Application
 * Provides enhanced functionality and user experience improvements
 */

// DOM Content Loaded Event Listener
document.addEventListener('DOMContentLoaded', function() {
    console.log('Furnace Management System initialized');

    // Initialize all components
    initializeFormValidation();
    initializeTooltips();
    initializeTableEnhancements();
    initializeTimestampHandling();
    initializeMaterialCalculations();
    initializeAutoSave();
    initializeKeyboardShortcuts();
    initializeCommaToDecimalPoint();
});

/**
 * Form Validation Enhancements
 */
function initializeFormValidation() {
    const forms = document.querySelectorAll('form');
    
    forms.forEach(form => {
        // Add real-time validation feedback
        const inputs = form.querySelectorAll('input, select, textarea');
        
        inputs.forEach(input => {
            input.addEventListener('blur', function() {
                validateField(this);
            });
            
            input.addEventListener('input', function() {
                clearFieldError(this);
            });
        });
        
        // Enhanced form submission
        form.addEventListener('submit', function(e) {
            if (!validateForm(this)) {
                e.preventDefault();
                showFormErrors(this);
            }
        });
    });
}

/**
 * Validate individual form field
 */
function validateField(field) {
    const fieldType = field.type;
    const value = field.value.trim();
    let isValid = true;
    let errorMessage = '';
    
    // Required field validation
    if (field.hasAttribute('required') && !value) {
        isValid = false;
        errorMessage = 'This field is required';
    }
    
    // Number field validation
    if (fieldType === 'number' && value) {
        const min = parseFloat(field.getAttribute('min'));
        const max = parseFloat(field.getAttribute('max'));
        const numValue = parseFloat(value);
        
        if (isNaN(numValue)) {
            isValid = false;
            errorMessage = 'Please enter a valid number';
        } else if (min !== null && numValue < min) {
            isValid = false;
            errorMessage = `Value must be at least ${min}`;
        } else if (max !== null && numValue > max) {
            isValid = false;
            errorMessage = `Value must be no more than ${max}`;
        }
    }
    
    // Email validation
    if (fieldType === 'email' && value) {
        const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
        if (!emailRegex.test(value)) {
            isValid = false;
            errorMessage = 'Please enter a valid email address';
        }
    }
    
    // Display validation result
    if (!isValid) {
        showFieldError(field, errorMessage);
    } else {
        clearFieldError(field);
    }
    
    return isValid;
}

/**
 * Show field-specific error message
 */
function showFieldError(field, message) {
    clearFieldError(field);
    
    field.classList.add('is-invalid');
    
    const errorDiv = document.createElement('div');
    errorDiv.className = 'invalid-feedback';
    errorDiv.textContent = message;
    
    field.parentNode.appendChild(errorDiv);
}

/**
 * Clear field error styling and message
 */
function clearFieldError(field) {
    field.classList.remove('is-invalid');
    
    const existingError = field.parentNode.querySelector('.invalid-feedback');
    if (existingError) {
        existingError.remove();
    }
}

/**
 * Validate entire form
 */
function validateForm(form) {
    let isValid = true;
    const fields = form.querySelectorAll('input, select, textarea');
    
    fields.forEach(field => {
        if (!validateField(field)) {
            isValid = false;
        }
    });
    
    return isValid;
}

/**
 * Show form-level errors
 */
function showFormErrors(form) {
    const invalidFields = form.querySelectorAll('.is-invalid');
    if (invalidFields.length > 0) {
        invalidFields[0].focus();
        showNotification('Please correct the errors in the form', 'error');
    }
}

/**
 * Initialize Bootstrap tooltips
 */
function initializeTooltips() {
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.map(function(tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });
}

/**
 * Table Enhancement Features
 */
function initializeTableEnhancements() {
    const tables = document.querySelectorAll('.table');
    
    tables.forEach(table => {
        // Add row highlighting on hover
        addTableRowHighlighting(table);
        
        // Add sortable columns if not already implemented
        addTableSorting(table);
        
        // Add row selection for bulk operations
        addTableRowSelection(table);
    });
}

/**
 * Add table row highlighting
 */
function addTableRowHighlighting(table) {
    const rows = table.querySelectorAll('tbody tr');
    
    rows.forEach(row => {
        row.addEventListener('mouseenter', function() {
            this.style.backgroundColor = 'rgba(44, 62, 80, 0.05)';
        });
        
        row.addEventListener('mouseleave', function() {
            this.style.backgroundColor = '';
        });
    });
}

/**
 * Simple table sorting functionality
 */
function addTableSorting(table) {
    const headers = table.querySelectorAll('th');
    
    headers.forEach((header, index) => {
        if (header.textContent.trim() && !header.querySelector('input')) {
            header.style.cursor = 'pointer';
            header.addEventListener('click', function() {
                sortTable(table, index);
            });
        }
    });
}

/**
 * Sort table by column
 */
function sortTable(table, columnIndex) {
    const tbody = table.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    
    // Determine sort direction
    const header = table.querySelectorAll('th')[columnIndex];
    const isAscending = !header.classList.contains('sort-desc');
    
    // Clear previous sort indicators
    table.querySelectorAll('th').forEach(th => {
        th.classList.remove('sort-asc', 'sort-desc');
    });
    
    // Add current sort indicator
    header.classList.add(isAscending ? 'sort-asc' : 'sort-desc');
    
    // Sort rows
    rows.sort((a, b) => {
        const aVal = a.cells[columnIndex].textContent.trim();
        const bVal = b.cells[columnIndex].textContent.trim();
        
        // Try numeric comparison first
        const aNum = parseFloat(aVal);
        const bNum = parseFloat(bVal);
        
        if (!isNaN(aNum) && !isNaN(bNum)) {
            return isAscending ? aNum - bNum : bNum - aNum;
        }
        
        // Fallback to string comparison
        return isAscending ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
    });
    
    // Reorder DOM elements
    rows.forEach(row => tbody.appendChild(row));
}

/**
 * Add row selection for bulk operations
 */
function addTableRowSelection(table) {
    const rows = table.querySelectorAll('tbody tr');
    
    rows.forEach(row => {
        row.addEventListener('click', function(e) {
            // Skip if clicking on buttons or links
            if (e.target.closest('button, a')) return;
            
            this.classList.toggle('table-active');
        });
    });
}

/**
 * Timestamp Handling
 */
function initializeTimestampHandling() {
    const timestampButtons = document.querySelectorAll('[data-timestamp]');
    
    timestampButtons.forEach(button => {
        button.addEventListener('click', function() {
            const action = this.getAttribute('data-timestamp');
            handleTimestamp(action, this);
        });
    });
}

/**
 * Handle timestamp recording
 */
function handleTimestamp(action, button) {
    const now = new Date();
    const timeString = now.toLocaleTimeString();
    
    // Disable button
    button.disabled = true;
    button.innerHTML = `<i class="fas fa-check me-1"></i>Recorded`;
    
    // Show timestamp
    const timestampDisplay = button.parentNode.querySelector('.timestamp-display');
    if (timestampDisplay) {
        timestampDisplay.textContent = timeString;
        timestampDisplay.style.display = 'block';
    }
    
    showNotification(`${action.replace('_', ' ')} recorded at ${timeString}`, 'success');
}

/**
 * Material Calculations
 */
function initializeMaterialCalculations() {
    const materialInputs = document.querySelectorAll('.material-section input[type="number"]');
    
    if (materialInputs.length > 0) {
        // Add total calculation display
        addMaterialTotalDisplay();
        
        // Update totals when inputs change
        materialInputs.forEach(input => {
            input.addEventListener('input', updateMaterialTotals);
        });
        
        // Initial calculation
        updateMaterialTotals();
    }
}

/**
 * Add material total display
 */
function addMaterialTotalDisplay() {
    const baseMaterialsCard = document.querySelector('.card .card-header h5');
    if (baseMaterialsCard && baseMaterialsCard.textContent.includes('Base Materials')) {
        const cardBody = baseMaterialsCard.closest('.card').querySelector('.card-body');
        
        const totalDiv = document.createElement('div');
        totalDiv.className = 'mt-3 p-3 bg-light border rounded';
        totalDiv.innerHTML = `
            <div class="row">
                <div class="col-md-6">
                    <h6 class="mb-1">Base Materials Total:</h6>
                    <span id="base-total" class="h5 text-primary">0.00 tons</span>
                </div>
                <div class="col-md-6">
                    <h6 class="mb-1">Grand Total:</h6>
                    <span id="grand-total" class="h5 text-success">0.00 tons</span>
                </div>
            </div>
        `;
        
        cardBody.appendChild(totalDiv);
    }
}

/**
 * Update material totals
 */
function updateMaterialTotals() {
    // Base materials (in tons)
    const baseMaterials = [
        'cast_iron', 'steel_scrap', 'pig_iron'
    ];
    
    // Additional materials (in kg, convert to tons)
    const additionalMaterials = [
        'recarb', 'ferrosilicon', 'ferromanganese', 'iron_sulfide',
        'additional_recarb', 'additional_fesi', 'additional_femn', 
        'additional_iron_sulfide', 'tin', 'copper'
    ];
    
    let baseTotal = 0;
    let additionalTotal = 0;
    
    // Calculate base materials total
    baseMaterials.forEach(material => {
        const input = document.querySelector(`input[name="${material}"]`);
        if (input) {
            baseTotal += parseFloat(input.value) || 0;
        }
    });
    
    // Calculate additional materials total (convert kg to tons)
    additionalMaterials.forEach(material => {
        const input = document.querySelector(`input[name="${material}"]`);
        if (input) {
            additionalTotal += (parseFloat(input.value) || 0) / 1000; // Convert kg to tons
        }
    });
    
    const grandTotal = baseTotal + additionalTotal;
    
    // Update display
    const baseTotalDisplay = document.getElementById('base-total');
    const grandTotalDisplay = document.getElementById('grand-total');
    
    if (baseTotalDisplay) {
        baseTotalDisplay.textContent = `${baseTotal.toFixed(2)} tons`;
    }
    
    if (grandTotalDisplay) {
        grandTotalDisplay.textContent = `${grandTotal.toFixed(2)} tons`;
    }
}

/**
 * Auto-save functionality for forms
 */
function initializeAutoSave() {
    const forms = document.querySelectorAll('form[data-autosave]');
    
    forms.forEach(form => {
        const inputs = form.querySelectorAll('input, select, textarea');
        
        inputs.forEach(input => {
            input.addEventListener('change', function() {
                saveFormData(form);
            });
        });
        
        // Load saved data on page load
        loadFormData(form);
    });
}

/**
 * Save form data to localStorage
 */
function saveFormData(form) {
    const formData = new FormData(form);
    const data = {};
    
    for (let [key, value] of formData.entries()) {
        data[key] = value;
    }
    
    const formId = form.id || 'default-form';
    localStorage.setItem(`furnace-form-${formId}`, JSON.stringify(data));
}

/**
 * Load form data from localStorage
 */
function loadFormData(form) {
    const formId = form.id || 'default-form';
    const savedData = localStorage.getItem(`furnace-form-${formId}`);
    
    if (savedData) {
        const data = JSON.parse(savedData);
        
        Object.keys(data).forEach(key => {
            const input = form.querySelector(`[name="${key}"]`);
            if (input && !input.value) {
                input.value = data[key];
            }
        });
    }
}

/**
 * Keyboard Shortcuts
 */
function initializeKeyboardShortcuts() {
    document.addEventListener('keydown', function(e) {
        // Ctrl/Cmd + S to save form
        if ((e.ctrlKey || e.metaKey) && e.key === 's') {
            e.preventDefault();
            const submitButton = document.querySelector('button[type="submit"]');
            if (submitButton) {
                submitButton.click();
            }
        }
        
        // Escape to cancel/go back
        if (e.key === 'Escape') {
            const cancelButton = document.querySelector('a[href*="back"], .btn-secondary');
            if (cancelButton) {
                cancelButton.click();
            }
        }
        
        // Ctrl/Cmd + N for new entry
        if ((e.ctrlKey || e.metaKey) && e.key === 'n') {
            e.preventDefault();
            const newButton = document.querySelector('a[href*="new"]');
            if (newButton) {
                newButton.click();
            }
        }
    });
}

/**
 * Show notification to user
 */
function showNotification(message, type = 'info') {
    // Remove existing notifications
    const existingNotifications = document.querySelectorAll('.notification-toast');
    existingNotifications.forEach(notification => notification.remove());
    
    // Create new notification
    const notification = document.createElement('div');
    notification.className = `alert alert-${type} notification-toast position-fixed`;
    notification.style.cssText = `
        top: 20px;
        right: 20px;
        z-index: 1050;
        min-width: 300px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    `;
    notification.innerHTML = `
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    `;
    
    document.body.appendChild(notification);
    
    // Auto-remove after 5 seconds
    setTimeout(() => {
        if (notification.parentNode) {
            notification.remove();
        }
    }, 5000);
}

/**
 * Utility function to format numbers
 */
function formatNumber(num, decimals = 2) {
    return Number(num).toFixed(decimals);
}

/**
 * Utility function to debounce function calls
 */
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

/**
 * Comma to decimal point conversion for numeric inputs.
 * Intercepts keystrokes and handles paste so users can type either , or . as the decimal separator.
 */
function initializeCommaToDecimalPoint() {
    const SKIP_TYPES = new Set(['date', 'hidden', 'checkbox', 'radio', 'file', 'password',
                                 'submit', 'button', 'reset', 'color', 'range', 'search']);

    function isNumericInput(el) {
        return el.tagName === 'INPUT' && !SKIP_TYPES.has(el.type);
    }

    // Intercept comma keystroke — prevent it and insert a period at the cursor instead
    document.addEventListener('keydown', function(e) {
        if (e.key !== ',') return;
        const el = e.target;
        if (!isNumericInput(el)) return;
        e.preventDefault();
        const start = el.selectionStart;
        const end   = el.selectionEnd;
        el.value = el.value.substring(0, start) + '.' + el.value.substring(end);
        el.setSelectionRange(start + 1, start + 1);
        // Re-fire input so any live calculators (e.g. tin/copper) still update
        el.dispatchEvent(new Event('input', { bubbles: true }));
    }, true); // capture phase so this runs before other listeners

    // Handle paste — if pasted text contains commas, replace them after insertion
    document.addEventListener('input', function(e) {
        const el = e.target;
        if (!isNumericInput(el) || !el.value.includes(',')) return;
        const pos = el.selectionStart;
        el.value = el.value.replace(/,/g, '.');
        el.setSelectionRange(pos, pos);
    });
}

// Export functions for use in other scripts
window.FurnaceManagement = {
    showNotification,
    formatNumber,
    debounce,
    validateField,
    updateMaterialTotals
};
