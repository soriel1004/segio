<?php
/* PHP 5.3 compatible. Identity comes only from SEGIO's native request context.
 * No DB connection, password handling, user-supplied target, or shell commands.
 */
ini_set('display_errors', '0');
header('Cache-Control: no-store, private');
header('X-Content-Type-Options: nosniff');

function reply($status, $body) {
    $reasons = array(200=>'OK', 400=>'Bad Request', 401=>'Unauthorized',
                     404=>'Not Found', 405=>'Method Not Allowed', 409=>'Conflict',
                     503=>'Service Unavailable');
    header('HTTP/1.1 '.$status.' '.$reasons[$status]);
    header('Content-Type: application/json; charset=UTF-8');
    echo json_encode($body);
    exit;
}
function fail_bridge($status, $code) {
    reply($status, array('protocol'=>'segio-login-archive/1', 'error'=>$code));
}
function read_object($path) {
    if (!is_file($path) || is_link($path) || filesize($path)>65536) return false;
    return json_decode(file_get_contents($path), true);
}

/* Legacy register_globals could overwrite native superglobals from a request. */
if (in_array(strtolower((string)ini_get('register_globals')),
             array('1','on','yes','true'), true))
    fail_bridge(503, 'insecure_php_configuration');

/* SEGIO source uses $_ENV['REMOTE_USER']. Standard CGI may use $_SERVER.
 * HTTP_REMOTE_USER, PHP_AUTH_USER, query/form/cookie IDs are NEVER identities.
 * No native identity => fail closed; do not add request-header fallbacks.
 */
$env_uid = isset($_ENV['REMOTE_USER']) ? $_ENV['REMOTE_USER'] : '';
$server_uid = isset($_SERVER['REMOTE_USER']) ? $_SERVER['REMOTE_USER'] : '';
if ($env_uid !== '' && $server_uid !== '' && $env_uid !== $server_uid)
    fail_bridge(401, 'identity_conflict');
$uid = $env_uid !== '' ? $env_uid : $server_uid;
if (!is_string($uid) || !preg_match('/\A[A-Za-z0-9_.@-]{1,100}\z/', $uid) ||
    strtolower($uid)==='guest') fail_bridge(401, 'native_login_required');
if (isset($_ENV['REMOTE_USERLEVEL']) && (string)$_ENV['REMOTE_USERLEVEL']==='0')
    fail_bridge(401, 'native_login_required');

$method = isset($_SERVER['REQUEST_METHOD']) ? $_SERVER['REQUEST_METHOD'] :
          (isset($_ENV['REQUEST_METHOD']) ? $_ENV['REQUEST_METHOD'] : '');
if ($method!=='GET' && $method!=='HEAD') fail_bridge(405, 'read_only');
foreach ($_GET as $key=>$value) {
    if ($key!=='action' && $key!=='version') fail_bridge(400, 'unexpected_parameter');
}
$action = isset($_GET['action']) ? $_GET['action'] : 'info';
if (!is_string($action) || !in_array($action, array('info','download'), true))
    fail_bridge(400, 'unknown_action');

$root = '__PRIVATE_ROOT__';
$record = read_object($root.'/users/'.hash('sha256', $uid).'.json');
if (!$record) {
    if ($action==='download') fail_bridge(404, 'archive_not_prepared');
    reply(200, array('protocol'=>'segio-login-archive/1', 'username'=>$uid, 'archive'=>null));
}
if (!isset($record['username'], $record['version'], $record['bytes'], $record['sha256']) ||
    $record['username']!==$uid ||
    !preg_match('/\A[a-f0-9]{32}\z/', $record['version']) ||
    !preg_match('/\A[a-f0-9]{64}\z/', $record['sha256']) || $record['bytes']<=0)
    fail_bridge(503, 'invalid_publication');
$file = $root.'/snapshots/'.$record['version'].'/archive.zip';
if (is_link($file) || !is_file($file) || filesize($file)!=$record['bytes'])
    fail_bridge(503, 'publication_unavailable');
if ($action==='info') {
    unset($record['username']);
    reply(200, array('protocol'=>'segio-login-archive/1', 'username'=>$uid, 'archive'=>$record));
}
if (!isset($_GET['version']) || $_GET['version']!==$record['version'])
    fail_bridge(409, 'archive_updated');
$stream = fopen($file, 'rb');
if (!$stream) fail_bridge(503, 'publication_unavailable');
header('Content-Type: application/zip');
header('Content-Disposition: attachment; filename="segio-'.$uid.'.zip"');
header('Content-Length: '.$record['bytes']);
header('ETag: "'.$record['sha256'].'"');
if ($method==='HEAD') { fclose($stream); exit; }
@set_time_limit(0);
while (!feof($stream) && !connection_aborted()) {
    $block = fread($stream, 1048576);
    if ($block===false || $block==='') break;
    echo $block;
    flush();
}
fclose($stream);
