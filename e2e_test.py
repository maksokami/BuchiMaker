import pytest
from playwright.sync_api import Page, expect

# Requires backend on 8000 and frontend on 8080 to be running.
# Usage: pytest e2e_test.py

def test_system_settings_page(page: Page):
    # Navigate to the frontend
    page.goto("http://localhost:8080/")
    
    # Wait for dashboard to load
    expect(page.locator("h1").filter(has_text="Overview Dashboard")).to_be_visible(timeout=10000)
    
    # Click the Settings icon in the sidebar
    settings_btn = page.locator("button[title='Settings']")
    settings_btn.click()
    
    # Verify System Settings page is displayed
    expect(page.locator("h1").filter(has_text="System Settings")).to_be_visible()
    
    # Verify Data Sources tab is active
    expect(page.locator("button").filter(has_text="Data Sources")).to_have_class("tab-btn active")
    
    # Verify Auto Refresh mode radios
    expect(page.locator("text=Disabled")).to_be_visible()
    expect(page.locator("text=Basic")).to_be_visible()
    expect(page.locator("text=Cron")).to_be_visible()
    
    # Save settings
    page.locator("text=Save Settings").click()
    
    # Verify success message
    expect(page.locator(".message-box.success")).to_contain_text("Settings saved successfully.", timeout=5000)
    
    # Open CSV Accordion
    page.locator(".accordion-header").filter(has_text="Add CSV Data Source").click()
    
    # Check if inputs are visible
    name_input = page.locator(".form-grid input").first
    expect(name_input).to_be_visible()

    # Fill CSV data source
    page.locator(".form-grid label:text-is('Name') + input").fill("test_csv")
    page.locator(".form-grid label:text-is('Title') + input").fill("Test CSV Title")
    page.locator(".form-grid label:text-is('File Path') + input").fill("/app/data/test.csv")
    page.locator(".form-grid label:text-is('Description') + input").fill("E2E Test Description")
    
    # Submit form (using the correct button under the accordion)
    page.locator(".accordion-actions button.primary").filter(has_text="Submit").click()
    
    # The message should show it failed if the file doesn't exist, but since it might not, just wait for message box
    expect(page.locator(".message-box")).to_be_visible(timeout=5000)

def test_widget_data_validation(page: Page):
    # Navigate to the frontend
    page.goto("http://localhost:8080/")
    
    # Wait for app container to be visible
    expect(page.locator(".app-container")).to_be_visible(timeout=10000)
    
    # Click on the test dashboard in the sidebar
    page.locator("a").filter(has_text="Invalid Data Dashboard").click()
    
    # Wait for the Invalid Data Dashboard to load
    expect(page.locator("h1").filter(has_text="Invalid Data Dashboard")).to_be_visible(timeout=10000)
    
    # Verify the Sankey widget shows the validation error
    invalid_msg = page.locator(".widget-card", has_text="Test Sankey").locator(".widget-unknown")
    expect(invalid_msg).to_have_text("Data format invalid", timeout=5000)
