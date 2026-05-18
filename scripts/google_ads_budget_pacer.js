/**
 * ============================================================================
 * GOOGLE ADS MCC BUDGET PACER WITH COMPOSITE SEGMENTATION & ENDED CAMPAIGN FIX
 * ============================================================================
 *
 * HISTORICAL REFERENCE — This script powered the original Supabase-based
 * pipeline before BudgetBuddy replaced it. Kept here for:
 *   - understanding the business rules ported into the Flask backend
 *   - reference if manual one-off runs are ever needed
 *
 * The logic rules from this script are now implemented in:
 *   backend/google_ads_client.py  — channel filtering, spend queries
 *   backend/routes/pacing.py      — grant bypass, dead-campaign protection
 *   backend/routes/sheets.py      — Google Ads section reader, segment filter
 *
 * DO NOT deploy this script as the primary automation while BudgetBuddy is
 * active — duplicate runs will double-count spend in the sheet.
 * ============================================================================
 */

// =============================================================================
// USER CONFIGURATION
// =============================================================================
var SPREADSHEET_ID = "1qCkWlqT21K1tHtSYmN3HPGKBKZLSQ84qmpDIC8O3VYA";
var SHEET_NAME = "May 2026";
var WEBHOOK_URL = "https://dksqlsguueaxnrkkvnyz.supabase.co/functions/v1/google-ads-webhook";
var WEBHOOK_API_KEY = "Commit-Vault-2026-XyZ7";
var EMAIL_ADDRESS = "jruiz@commitagency.com";
var SEND_EMAIL_NOTIFICATIONS = true;
var BUDGET_THRESHOLD = 1.0; // 100% Budget Utilization Cap
var LABEL_NAME = "Auto-Paused-Budget";
var LABEL_DESCRIPTION = "Paused by Budget Pacer Script";
var LABEL_COLOR = "#FF0000";
var WEBHOOK_TIMEOUT_MS = 30000;
var WEBHOOK_RETRY_ATTEMPTS = 2;

// =============================================================================
// MAIN FUNCTION - ENTRY POINT
// =============================================================================
function main() {
  Logger.log("========================================");
  Logger.log("BUDGET PACER SCRIPT STARTED");
  Logger.log("Timestamp: " + new Date().toISOString());
  Logger.log("Threshold: " + (BUDGET_THRESHOLD * 100) + "%");
  Logger.log("========================================");
  var budgetData = loadBudgetDataFromSheet();
  if (!budgetData) {
    Logger.log("ERROR: Failed to load budget data. Exiting.");
    return;
  }
  var dateRange = calculateDateRange();
  Logger.log("Date Range: " + dateRange.startDate + " to " + dateRange.endDate);
  var accountIterator = AdsManagerApp.accounts().get();
  var processedCount = 0;
  var pausedCount = 0;
  while (accountIterator.hasNext()) {
    var account = accountIterator.next();
    var accountNameLower = account.getName().toLowerCase().trim();
    if (budgetData.hasOwnProperty(accountNameLower)) {
      processedCount++;
      var result = processAccount(account, budgetData[accountNameLower], dateRange);
      if (result.campaignsPaused > 0) {
        pausedCount++;
      }
    }
  }
  Logger.log("========================================");
  Logger.log("SCRIPT COMPLETED. Processed: " + processedCount + " | Paused: " + pausedCount);
  Logger.log("========================================");
}

// =============================================================================
// BUDGET DATA LOADING
// =============================================================================
function loadBudgetDataFromSheet() {
  try {
    Logger.log("Loading budget data from spreadsheet...");
    var spreadsheet = SpreadsheetApp.openById(SPREADSHEET_ID);
    var sheet = spreadsheet.getSheetByName(SHEET_NAME);

    if (!sheet) {
      Logger.log("ERROR: Sheet '" + SHEET_NAME + "' not found.");
      return null;
    }
    var lastRow = sheet.getLastRow();
    if (lastRow < 2) return {};
    var data = sheet.getRange(1, 1, lastRow, 4).getValues();
    var budgetMap = {};
    for (var i = 1; i < data.length; i++) {
      var accountName = data[i][0].toString().trim();
      if (accountName) {
        var accountNameLower = accountName.toLowerCase();
        if (!budgetMap[accountNameLower]) {
          budgetMap[accountNameLower] = [];
        }
        budgetMap[accountNameLower].push({
          originalName: accountName,
          campaignFilter: data[i][1].toString().trim().toLowerCase(),
          budget: parseFloat(data[i][2]) || 0,
          rowNumber: i + 1,
          sheet: sheet
        });
      }
    }
    return budgetMap;
  } catch (e) {
    Logger.log("ERROR loading spreadsheet: " + e.message);
    return null;
  }
}

// =============================================================================
// DATE RANGE CALCULATION
// =============================================================================
function calculateDateRange() {
  var today = new Date();
  var firstOfMonth = new Date(today.getFullYear(), today.getMonth(), 1);
  var yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);

  var endDate = (yesterday.getMonth() !== today.getMonth()) ? today : yesterday;

  return {
    startDate: Utilities.formatDate(firstOfMonth, "GMT", "yyyyMMdd"),
    endDate: Utilities.formatDate(endDate, "GMT", "yyyyMMdd")
  };
}

// =============================================================================
// ACCOUNT PROCESSING
// =============================================================================
function processAccount(account, budgetConfigs, dateRange) {
  var result = { accountId: "", accountName: "", spend: 0, budget: 0, campaignsPaused: 0, error: null };
  try {
    AdsManagerApp.select(account);
    var accountName = account.getName();
    var accountId = account.getCustomerId();
    var timezone = account.getTimeZone();

    result.accountId = accountId;
    result.accountName = accountName;

    // INTEGRATED GUARDRAIL: Automatically bypass Google Grant accounts
    var isGrantAccount = accountName.toLowerCase().indexOf("grant") !== -1;
    Logger.log(">>> Processing: " + accountName + " (" + accountId + ")" + (isGrantAccount ? " [GRANT ACCOUNT - EXEMPT]" : ""));

    var labelReady = ensureLabelExists();
    if (!labelReady) return result;

    var allSegmentLabels = [];
    var otherFilters = [];
    for (var i = 0; i < budgetConfigs.length; i++) {
      var label = budgetConfigs[i].campaignFilter || "Primary";
      allSegmentLabels.push(label);
      if (budgetConfigs[i].campaignFilter && budgetConfigs[i].campaignFilter !== "") {
        otherFilters.push(budgetConfigs[i].campaignFilter.toLowerCase());
      }
    }

    var enabledBudgetShareCounts = getEnabledBudgetShareCounts();
    for (var i = 0; i < budgetConfigs.length; i++) {
      var config = budgetConfigs[i];
      var segmentLabel = config.campaignFilter || "Primary";
      var metrics;
      var isEmptyFilterInMultiSegment = (!config.campaignFilter || config.campaignFilter === "") && otherFilters.length > 0;

      if (isEmptyFilterInMultiSegment) {
        metrics = calculateSpendExcluding(otherFilters, dateRange, enabledBudgetShareCounts);
      } else {
        metrics = calculateSpend(config.campaignFilter, dateRange, enabledBudgetShareCounts);
      }

      var spend = metrics.spend;
      updateSpendInSheet(config.sheet, config.rowNumber, spend);

      Logger.log("    Segment: " + segmentLabel + " | Spend: $" + spend.toFixed(2) + " / Budget: $" + config.budget);

      var effectiveBudget = config.budget * BUDGET_THRESHOLD;
      var segmentExceeded = !isGrantAccount && config.budget > 0 && spend >= effectiveBudget;

      if (segmentExceeded) {
        Logger.log("    ⚠️ SEGMENT THRESHOLD EXCEEDED!");
        var pauseResult;
        if (isEmptyFilterInMultiSegment) {
          pauseResult = pauseCampaignsExcluding(otherFilters);
        } else {
          pauseResult = pauseFilteredCampaigns(config.campaignFilter);
        }
        result.campaignsPaused += pauseResult.pausedCampaigns.length;

        if (pauseResult.pausedCampaigns.length > 0) {
          sendWebhookNotification({
            accountId: accountId, accountName: accountName, spend: spend, budget: config.budget,
            threshold: BUDGET_THRESHOLD, pausedCampaigns: pauseResult.pausedCampaigns, timezone: timezone,
            eventType: "BUDGET_EXCEEDED", budgetLabel: segmentLabel, batchLabels: allSegmentLabels,
            clicks: metrics.clicks, conversions: metrics.conversions, cpc: metrics.cpc, campaignBreakdown: metrics.campaigns || []
          });
          if (SEND_EMAIL_NOTIFICATIONS) {
            sendEmailNotification(accountName + " [" + segmentLabel + "]", spend, config.budget, pauseResult.pausedCampaigns);
          }
        }
      } else {
        sendWebhookNotification({
          accountId: accountId, accountName: accountName, spend: spend, budget: config.budget,
          threshold: BUDGET_THRESHOLD, pausedCampaigns: [], timezone: timezone,
          eventType: "STATUS_UPDATE", budgetLabel: segmentLabel, batchLabels: allSegmentLabels,
          clicks: metrics.clicks, conversions: metrics.conversions, cpc: metrics.cpc, campaignBreakdown: metrics.campaigns || []
        });
      }
    }
    SpreadsheetApp.flush();
  } catch (e) {
    result.error = e.message;
    Logger.log("    ERROR: " + e.message);
  }
  return result;
}

// =============================================================================
// LABEL MANAGEMENT
// =============================================================================
function ensureLabelExists() {
  var labelIterator = AdsApp.labels().withCondition("Name = '" + LABEL_NAME + "'").get();
  if (labelIterator.hasNext()) return true;
  AdsApp.createLabel(LABEL_NAME, LABEL_DESCRIPTION, LABEL_COLOR);
  return false;
}

// =============================================================================
// SPEND & SHARE ACCOUNT LEVEL PROCESSING
// =============================================================================
function getEnabledBudgetShareCounts() {
  var counts = {};
  var query = "SELECT campaign.name, campaign.status, campaign.advertising_channel_type, campaign_budget.id FROM campaign WHERE campaign.status = 'ENABLED'";
  var report = AdsApp.report(query);
  var rows = report.rows();
  var seenCampaigns = {};
  while (rows.hasNext()) {
    var row = rows.next();
    var campaignName = row['campaign.name'];
    var budgetId = row['campaign_budget.id'] || '';
    var channelType = (row['campaign.advertising_channel_type'] || '').toUpperCase();
    if (!budgetId || seenCampaigns[campaignName]) continue;
    // PHANTOM BUDGET FIX: exclude non-standard channel types
    if (channelType === 'LOCAL_SERVICES' || channelType === 'SMART' || channelType === 'HOTEL' || channelType === 'LOCAL') continue;
    seenCampaigns[campaignName] = true;
    counts[budgetId] = (counts[budgetId] || 0) + 1;
  }
  return counts;
}

function calculateSpend(campaignFilter, dateRange, enabledBudgetShareCounts) {
  var totalSpend = 0; var totalClicks = 0; var totalConversions = 0;
  var campaignMap = {};
  var query = "SELECT campaign.name, campaign.status, campaign.advertising_channel_type, campaign_budget.id, campaign_budget.amount_micros, metrics.cost_micros, metrics.clicks, metrics.conversions " +
              "FROM campaign " +
              "WHERE segments.date BETWEEN '" + dateRange.startDate + "' AND '" + dateRange.endDate + "'";

  var report = AdsApp.report(query);
  var rows = report.rows();
  while (rows.hasNext()) {
    var row = rows.next();
    var rowCampaignName = row['campaign.name'];
    var channelType = (row['campaign.advertising_channel_type'] || '').toUpperCase();
    // PHANTOM BUDGET FIX
    if (channelType === 'LOCAL_SERVICES' || channelType === 'SMART' || channelType === 'HOTEL' || channelType === 'LOCAL') continue;
    if (campaignFilter && campaignFilter !== "" && rowCampaignName.toLowerCase().indexOf(campaignFilter) === -1) continue;
    var costMicros = parseFloat(row['metrics.cost_micros']) || 0;
    var clicks = parseInt(row['metrics.clicks'], 10) || 0;
    var conversions = parseFloat(row['metrics.conversions']) || 0;
    var dailyBudgetMicros = parseFloat(row['campaign_budget.amount_micros']) || 0;
    var status = row['campaign.status'] || 'ENABLED';
    var budgetId = row['campaign_budget.id'] || '';
    totalSpend += costMicros / 1000000;
    totalClicks += clicks;
    totalConversions += conversions;
    if (!campaignMap[rowCampaignName]) {
      campaignMap[rowCampaignName] = { name: rowCampaignName, spend: 0, clicks: 0, conversions: 0, cpc: 0, status: status, daily_budget: dailyBudgetMicros / 1000000, budget_id: budgetId, budget_amount: dailyBudgetMicros / 1000000 };
    }
    campaignMap[rowCampaignName].spend += costMicros / 1000000;
    campaignMap[rowCampaignName].clicks += clicks;
    campaignMap[rowCampaignName].conversions += conversions;
  }
  return processCampaignMap(campaignMap, totalSpend, totalClicks, totalConversions, enabledBudgetShareCounts);
}

function calculateSpendExcluding(excludeFilters, dateRange, enabledBudgetShareCounts) {
  var totalSpend = 0; var totalClicks = 0; var totalConversions = 0;
  var campaignMap = {};
  var query = "SELECT campaign.name, campaign.status, campaign.advertising_channel_type, campaign_budget.id, campaign_budget.amount_micros, metrics.cost_micros, metrics.clicks, metrics.conversions " +
              "FROM campaign " +
              "WHERE segments.date BETWEEN '" + dateRange.startDate + "' AND '" + dateRange.endDate + "'";
  var report = AdsApp.report(query);
  var rows = report.rows();
  while (rows.hasNext()) {
    var row = rows.next();
    var rowCampaignName = row['campaign.name'];
    var campaignNameLower = rowCampaignName.toLowerCase();
    var channelType = (row['campaign.advertising_channel_type'] || '').toUpperCase();
    if (channelType === 'LOCAL_SERVICES' || channelType === 'SMART' || channelType === 'HOTEL' || channelType === 'LOCAL') continue;
    var belongsToOther = false;
    for (var f = 0; f < excludeFilters.length; f++) {
      if (campaignNameLower.indexOf(excludeFilters[f]) !== -1) {
        belongsToOther = true;
        break;
      }
    }
    if (!belongsToOther) {
      var costMicros = parseFloat(row['metrics.cost_micros']) || 0;
      var clicks = parseInt(row['metrics.clicks'], 10) || 0;
      var conversions = parseFloat(row['metrics.conversions']) || 0;
      var dailyBudgetMicros = parseFloat(row['campaign_budget.amount_micros']) || 0;
      var status = row['campaign.status'] || 'ENABLED';
      var budgetId = row['campaign_budget.id'] || '';
      totalSpend += costMicros / 1000000;
      totalClicks += clicks;
      totalConversions += conversions;
      if (!campaignMap[rowCampaignName]) {
        campaignMap[rowCampaignName] = { name: rowCampaignName, spend: 0, clicks: 0, conversions: 0, cpc: 0, status: status, daily_budget: dailyBudgetMicros / 1000000, budget_id: budgetId, budget_amount: dailyBudgetMicros / 1000000 };
      }
      campaignMap[rowCampaignName].spend += costMicros / 1000000;
      campaignMap[rowCampaignName].clicks += clicks;
      campaignMap[rowCampaignName].conversions += conversions;
    }
  }
  return processCampaignMap(campaignMap, totalSpend, totalClicks, totalConversions, enabledBudgetShareCounts);
}

function processCampaignMap(campaignMap, totalSpend, totalClicks, totalConversions, enabledBudgetShareCounts) {
  var budgetCounts = {};
  for (var n in campaignMap) {
    var cm = campaignMap[n];
    if ((cm.status || '').toUpperCase() !== 'ENABLED' || !cm.budget_id) continue;
    if (enabledBudgetShareCounts && enabledBudgetShareCounts[cm.budget_id] > 0) {
      budgetCounts[cm.budget_id] = enabledBudgetShareCounts[cm.budget_id];
    } else {
      budgetCounts[cm.budget_id] = (budgetCounts[cm.budget_id] || 0) + 1;
    }
  }
  var campaigns = [];
  for (var cName in campaignMap) {
    var c = campaignMap[cName];
    c.cpc = c.clicks > 0 ? Math.round((c.spend / c.clicks) * 100) / 100 : 0;
    c.spend = Math.round(c.spend * 100) / 100;
    c.conversions = Math.round(c.conversions);
    var share = (c.budget_id && budgetCounts[c.budget_id] > 0) ? budgetCounts[c.budget_id] : 1;
    var st2 = (c.status || '').toUpperCase();
    // DEAD CAMPAIGN PROTECTION: force daily_budget to $0 for non-ENABLED campaigns
    c.daily_budget = st2 === 'ENABLED' ? Math.round((c.budget_amount / share) * 100) / 100 : 0;
    delete c.budget_id;
    delete c.budget_amount;
    campaigns.push(c);
  }
  return {
    spend: totalSpend, clicks: totalClicks, conversions: Math.round(totalConversions),
    cpc: totalClicks > 0 ? Math.round((totalSpend / totalClicks) * 100) / 100 : 0, campaigns: campaigns
  };
}

// =============================================================================
// CAMPAIGN PAUSING ACTIONS
// =============================================================================
function pauseFilteredCampaigns(campaignFilter) {
  var pausedCampaigns = []; var skippedCampaigns = [];
  var campaignIterator = AdsApp.campaigns().withCondition("Status = ENABLED").get();
  while (campaignIterator.hasNext()) {
    var campaign = campaignIterator.next();
    var campaignName = campaign.getName();
    if (campaignFilter && campaignFilter !== "" && campaignName.toLowerCase().indexOf(campaignFilter.toLowerCase()) === -1) {
      continue;
    }
    try {
      campaign.applyLabel(LABEL_NAME);
      campaign.pause();
      pausedCampaigns.push(campaignName);
    } catch (e) {
      skippedCampaigns.push(campaignName);
    }
  }
  return { pausedCampaigns: pausedCampaigns, skippedCampaigns: skippedCampaigns };
}

function pauseCampaignsExcluding(excludeFilters) {
  var pausedCampaigns = []; var skippedCampaigns = [];
  var campaignIterator = AdsApp.campaigns().withCondition("Status = ENABLED").get();
  while (campaignIterator.hasNext()) {
    var campaign = campaignIterator.next();
    var campaignName = campaign.getName();
    var belongsToOther = false;
    for (var f = 0; f < excludeFilters.length; f++) {
      if (campaignName.toLowerCase().indexOf(excludeFilters[f]) !== -1) {
        belongsToOther = true;
        break;
      }
    }
    if (belongsToOther) continue;
    try {
      campaign.applyLabel(LABEL_NAME);
      campaign.pause();
      pausedCampaigns.push(campaignName);
    } catch (e) {
      skippedCampaigns.push(campaignName);
    }
  }
  return { pausedCampaigns: pausedCampaigns, skippedCampaigns: skippedCampaigns };
}

// =============================================================================
// SPREADSHEET & WEBHOOK INTEGRATION
// =============================================================================
function updateSpendInSheet(sheet, rowNumber, spend) {
  try { sheet.getRange(rowNumber, 4).setValue(spend); } catch (e) {}
}

function sendWebhookNotification(data) {
  if (!WEBHOOK_URL) return;
  // Composite unique_id to prevent segment overwrites
  var cleanLabel = (data.budgetLabel || "primary").toLowerCase().replace(/\s+/g, '_');
  var uniqueSegmentId = data.accountId + "_" + cleanLabel;
  var payload = {
    unique_id: uniqueSegmentId,
    current_month: SHEET_NAME,
    account_id: data.accountId,
    account_name: data.accountName,
    event_type: data.eventType || "ACCOUNT_PAUSED",
    spend: Math.round(data.spend * 100) / 100,
    budget: data.budget,
    threshold: data.threshold,
    spend_percentage: data.budget > 0 ? Math.round((data.spend / data.budget) * 10000) / 10000 : 0,
    paused_campaigns: data.pausedCampaigns || [],
    paused_count: data.pausedCampaigns ? data.pausedCampaigns.length : 0,
    timestamp: new Date().toISOString(),
    timezone: data.timezone,
    budget_label: data.budgetLabel || "Primary",
    batch_labels: data.batchLabels || [],
    clicks: data.clicks || 0,
    conversions: data.conversions || 0,
    cpc: data.cpc || 0,
    campaign_breakdown: data.campaignBreakdown || []
  };
  var options = {
    method: "post", contentType: "application/json",
    headers: { "x-api-key": WEBHOOK_API_KEY },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true, timeout: WEBHOOK_TIMEOUT_MS
  };
  var attempts = 0; var success = false;
  while (attempts < WEBHOOK_RETRY_ATTEMPTS && !success) {
    attempts++;
    try {
      var response = UrlFetchApp.fetch(WEBHOOK_URL, options);
      var responseCode = response.getResponseCode();
      if (responseCode >= 200 && responseCode < 300) success = true;
    } catch (e) {}
    if (!success && attempts < WEBHOOK_RETRY_ATTEMPTS) Utilities.sleep(1000);
  }
}

function sendEmailNotification(accountName, spend, budget, pausedCampaigns) {
  try {
    var subject = "🚨 BUDGET ALERT: " + accountName + " - Campaigns Paused";
    var body = "Account: " + accountName + "\nSpend: $" + spend.toFixed(2) + "\nBudget: $" + budget.toFixed(2) + "\n\nPaused Campaigns:\n- " + pausedCampaigns.join("\n- ");
    MailApp.sendEmail(EMAIL_ADDRESS, subject, body);
  } catch (e) {}
}
