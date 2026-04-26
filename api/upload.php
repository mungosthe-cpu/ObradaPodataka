<?php
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');

/**
 * Podesite ove vrednosti pre produkcije.
 * Mogu i preko hosting env varijabli (preporuka):
 * - TAURIS_SERVER_TOKEN
 * - TAURIS_SIGNING_KEY
 */
$serverToken = getenv('TAURIS_SERVER_TOKEN') ?: 'PROMENI_SERVER_TOKEN';
$signingKey = getenv('TAURIS_SIGNING_KEY') ?: 'PROMENI_SIGNING_KEY';

$uploadDir = __DIR__ . '/../server_uploads';
if (!is_dir($uploadDir)) {
    mkdir($uploadDir, 0775, true);
}

function respond(int $statusCode, array $body): void
{
    http_response_code($statusCode);
    echo json_encode($body, JSON_UNESCAPED_UNICODE);
    exit;
}

function getBearerToken(): string
{
    $header = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
    if (stripos($header, 'Bearer ') !== 0) {
        return '';
    }
    return trim(substr($header, 7));
}

function isAssoc(array $arr): bool
{
    if ($arr === []) {
        return false;
    }
    return array_keys($arr) !== range(0, count($arr) - 1);
}

function canonicalizeForHash($value)
{
    if (is_array($value)) {
        if (isAssoc($value)) {
            ksort($value);
        }
        foreach ($value as $k => $v) {
            $value[$k] = canonicalizeForHash($v);
        }
    }
    return $value;
}

function sanitizePart(string $text): string
{
    $clean = preg_replace('/[^A-Za-z0-9_-]+/', '_', $text);
    $clean = trim((string)$clean, '_');
    return $clean !== '' ? $clean : 'unknown';
}

if ($serverToken === 'PROMENI_SERVER_TOKEN' || $signingKey === 'PROMENI_SIGNING_KEY') {
    respond(500, [
        'ok' => false,
        'error' => 'Server nije konfigurisan. Podesite TAURIS_SERVER_TOKEN i TAURIS_SIGNING_KEY.',
    ]);
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    respond(405, ['ok' => false, 'error' => 'Dozvoljen je samo POST.']);
}

$bearer = getBearerToken();
if (!hash_equals($serverToken, $bearer)) {
    respond(401, ['ok' => false, 'error' => 'Nevalidan token.']);
}

if (!isset($_FILES['payload'], $_FILES['json_file'], $_FILES['pdf_file'])) {
    respond(400, ['ok' => false, 'error' => 'Nedostaju payload/json_file/pdf_file fajlovi.']);
}

$payloadRaw = file_get_contents($_FILES['payload']['tmp_name']);
if ($payloadRaw === false) {
    respond(400, ['ok' => false, 'error' => 'Ne mogu da procitam payload fajl.']);
}

$payload = json_decode($payloadRaw, true);
if (!is_array($payload)) {
    respond(400, ['ok' => false, 'error' => 'Payload nije validan JSON.']);
}

if (!isset($payload['race_data'], $payload['data_hash'], $payload['signature_hmac_sha256'])) {
    respond(400, ['ok' => false, 'error' => 'Payload nema obavezna polja race_data/data_hash/signature_hmac_sha256.']);
}

$canonicalRaceData = canonicalizeForHash($payload['race_data']);
$canonicalJson = json_encode($canonicalRaceData, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
if ($canonicalJson === false) {
    respond(400, ['ok' => false, 'error' => 'Ne mogu da kanonizujem race_data.']);
}

$realHash = hash('sha256', $canonicalJson);
$declaredHash = (string)$payload['data_hash'];
if (!hash_equals($declaredHash, $realHash)) {
    respond(400, ['ok' => false, 'error' => 'Hash mismatch - podaci su menjani.']);
}

$expectedSignature = hash_hmac('sha256', $realHash, $signingKey);
$declaredSignature = (string)$payload['signature_hmac_sha256'];
if (!hash_equals($declaredSignature, $expectedSignature)) {
    respond(400, ['ok' => false, 'error' => 'Nevalidan HMAC potpis.']);
}

$capturedAt = sanitizePart((string)($payload['meta']['captured_at'] ?? 'unknown_time'));
$port = sanitizePart((string)($payload['meta']['port'] ?? 'PORT'));
$baseName = $capturedAt . '_' . $port;

$jsonTarget = $uploadDir . '/' . $baseName . '.json';
$pdfTarget = $uploadDir . '/' . $baseName . '.pdf';
$payloadTarget = $uploadDir . '/' . $baseName . '_payload.json';

if (!move_uploaded_file($_FILES['json_file']['tmp_name'], $jsonTarget)) {
    respond(500, ['ok' => false, 'error' => 'Ne mogu da sacuvam json_file.']);
}
if (!move_uploaded_file($_FILES['pdf_file']['tmp_name'], $pdfTarget)) {
    respond(500, ['ok' => false, 'error' => 'Ne mogu da sacuvam pdf_file.']);
}
if (file_put_contents($payloadTarget, $payloadRaw) === false) {
    respond(500, ['ok' => false, 'error' => 'Ne mogu da sacuvam payload fajl.']);
}

respond(200, [
    'ok' => true,
    'message' => 'Upload uspesan i potpis validan.',
    'saved' => [
        'json' => $jsonTarget,
        'pdf' => $pdfTarget,
        'payload' => $payloadTarget,
    ],
]);
