/**
 * Apps Script — отправляет (push) содержимое листа "Просрок чистка" боту на VPS.
 *
 * Зачем именно push: таблица и web-приложения в домене MGCom нельзя открыть
 * анонимно, поэтому бот не может ЧИТАТЬ данные у Google. Но Apps Script может
 * сам слать ИСХОДЯЩИЕ запросы — он работает от твоего имени (доступ к таблице
 * есть) и по таймеру отправляет снапшот на HTTP-эндпоинт бота.
 *
 * Настройка:
 *   1. В таблице: Расширения → Apps Script, вставь этот код.
 *   2. Замени INGEST_URL на адрес твоего VPS (эндпоинт /ingest бота).
 *   3. Замени INGEST_TOKEN на тот же секрет, что в .env бота (INGEST_TOKEN).
 *   4. Запусти функцию pushSnapshot один раз вручную — Google попросит выдать
 *      разрешения (доступ к таблице и на внешние запросы). Согласись.
 *   5. Запусти функцию createTrigger один раз — это создаст таймер (каждые 5 минут).
 *
 * Развёртывание как Web App НЕ требуется.
 */

var SHEET_NAME = 'Просрок чистка';
var INGEST_URL = 'https://ТВОЙ_VPS:8080/ingest'; // адрес эндпоинта бота
var INGEST_TOKEN = 'ЗАМЕНИ_НА_ТОТ_ЖЕ_ТОКЕН_ЧТО_В_ENV';

function pushSnapshot() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  if (!sheet) {
    throw new Error('Лист не найден: ' + SHEET_NAME);
  }

  // getDisplayValues — ровно то, что видно в ячейках (даты как строки и т.п.).
  var values = sheet.getDataRange().getDisplayValues();
  var header = values.shift() || [];

  var payload = {
    updated_at: new Date().toISOString(),
    header: header,
    rows: values,
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
