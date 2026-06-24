/**
 * Apps Script — отправляет (push) содержимое листов таблицы боту на VPS.
 *
 * Зачем именно push: таблица и web-приложения в домене MGCom нельзя открыть
 * анонимно, поэтому бот не может ЧИТАТЬ данные у Google. Но Apps Script может
 * сам слать ИСХОДЯЩИЕ запросы — он работает от твоего имени (доступ к таблице
 * есть) и по таймеру отправляет снапшот на HTTP-эндпоинт бота.
 *
 * Настройка:
 *   1. В таблице: Расширения → Apps Script, вставь этот код.
 *   2. В SHEET_NAMES перечисли листы, которые нужно отправлять боту.
 *   3. Замени INGEST_URL на адрес твоего VPS (эндпоинт /ingest бота, http://).
 *   4. Замени INGEST_TOKEN на тот же секрет, что в .env бота (INGEST_TOKEN).
 *   5. Запусти функцию pushSnapshot один раз вручную — Google попросит выдать
 *      разрешения (доступ к таблице и на внешние запросы). Согласись.
 *   6. Запусти функцию createTrigger один раз — это создаст таймер (каждые 5 минут).
 *
 * Развёртывание как Web App НЕ требуется.
 */

// Листы для отправки. Имена должны совпадать с вкладками таблицы и со значениями
// PRIMARY_SHEET / DECISION_SHEET в .env бота.
var SHEET_NAMES = ['Просрок чистка', 'Принятие чистка'];
var INGEST_URL = 'http://ТВОЙ_VPS:8080/ingest'; // адрес эндпоинта бота (http, не https!)
var INGEST_TOKEN = 'ЗАМЕНИ_НА_ТОТ_ЖЕ_ТОКЕН_ЧТО_В_ENV';

function pushSnapshot() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheets = {};

  SHEET_NAMES.forEach(function (name) {
    var sheet = ss.getSheetByName(name);
    if (!sheet) {
      throw new Error('Лист не найден: ' + name);
    }
    // getDisplayValues — ровно то, что видно в ячейках (даты как строки и т.п.).
    var values = sheet.getDataRange().getDisplayValues();
    var header = values.shift() || [];
    sheets[name] = { header: header, rows: values };
  });

  var payload = {
    updated_at: new Date().toISOString(),
    sheets: sheets,
  };

  var response = UrlFetchApp.fetch(INGEST_URL, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-Auth-Token': INGEST_TOKEN },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });

  console.log('Ingest ответил: ' + response.getResponseCode() + ' ' + response.getContentText());
}

/** Создаёт таймер: pushSnapshot каждые 5 минут. Запустить один раз вручную. */
function createTrigger() {
  // Удаляем старые триггеры этой функции, чтобы не плодить дубликаты.
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'pushSnapshot') {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger('pushSnapshot').timeBased().everyMinutes(5).create();
  console.log('Триггер создан: pushSnapshot каждые 5 минут.');
}
