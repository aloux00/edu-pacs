<?php
/**
 * EduPACS 一键集成引导（在 Moodle 容器内执行）
 *
 * 自动完成：
 *   1. 等待 Moodle 安装结束
 *   2. 创建/复用教学课程与演示师生账号
 *   3. 创建/更新 LTI 1.3 预配置工具（EduPACS 为 Tool）
 *   4. 在课程内创建「影像阅片」外部工具活动
 *   5. 输出 lti-result.json，宿主脚本据此回写 EduPACS 的 deployment_id
 *
 * 所有参数来自 compose 环境变量；不写入任何硬编码密码。
 */

define('CLI_SCRIPT', true);
define('CACHE_DISABLE_ALL', true);

if (function_exists('opcache_reset')) {
    opcache_reset();
}

function eb_log($msg) {
    fwrite(STDOUT, date('H:i:s') . ' [edupacs-bootstrap] ' . $msg . PHP_EOL);
}

function eb_env($name, $default = '') {
    $v = getenv($name);
    return ($v === false || $v === '') ? $default : $v;
}

$configfile = '/bitnami/moodle/config.php';
for ($i = 0; $i < 60 && !file_exists($configfile); $i++) {
    eb_log('等待 Moodle config.php ...');
    sleep(2);
}
if (!file_exists($configfile)) {
    eb_log('错误：Moodle config.php 不存在');
    exit(1);
}

require($configfile);
require_once($CFG->libdir . '/moodlelib.php');
require_once($CFG->libdir . '/enrollib.php');
require_once($CFG->libdir . '/modinfolib.php');
require_once($CFG->dirroot . '/course/lib.php');
require_once($CFG->dirroot . '/course/modlib.php');
require_once($CFG->dirroot . '/mod/lti/locallib.php');

// 等待 Moodle 数据库安装完成（Bitnami 首次启动会自行 install）
$installed = false;
for ($i = 0; $i < 180; $i++) {
    try {
        if ($DB->record_exists('config', array('name' => 'version'))) {
            $installed = true;
            break;
        }
    } catch (Throwable $e) {
        // 表尚未建立
    }
    if ($i % 10 === 0) {
        eb_log('等待 Moodle 安装完成 ... (' . $i . ')');
    }
    sleep(3);
}
if (!$installed) {
    eb_log('错误：Moodle 安装超时，请查看 moodle 容器日志');
    exit(1);
}
eb_log('Moodle 已就绪：$CFG->wwwroot = ' . $CFG->wwwroot);

$clientid = eb_env('LTI_CLIENT_ID');
$toolbase = rtrim(eb_env('EDUPACS_LTI_PUBLIC_URL', 'http://localhost:8000'), '/');
$launchurl = $toolbase . '/lti/callback';
$loginurl = $toolbase . '/lti/login';
// 工具公钥由 Moodle 服务器端抓取，走 compose 内部服务名，避免容器 localhost 指向自身
$keyseturl = 'http://app:8000/lti/jwks.json';

if (!$clientid) {
    eb_log('错误：LTI_CLIENT_ID 未注入');
    exit(1);
}

// ---------------------------------------------------------------- 课程
$shortname = eb_env('MOODLE_COURSE_SHORTNAME', 'EduPACS2026');
$course = $DB->get_record('course', array('shortname' => $shortname));
if (!$course) {
    $newcourse = new stdClass();
    $newcourse->category = 1;
    $newcourse->fullname = eb_env('MOODLE_COURSE_FULLNAME', 'EduPACS 医学影像诊断学');
    $newcourse->shortname = $shortname;
    $newcourse->summary = '由 EduPACS 一键集成栈自动创建；课程内已配置 LTI 1.3 影像阅片活动。';
    $newcourse->format = 'topics';
    $newcourse->visible = 1;
    $course = create_course($newcourse);
    eb_log('已创建课程 id=' . $course->id);
} else {
    eb_log('复用课程 id=' . $course->id);
}

// ---------------------------------------------------------------- 演示账号
function eb_ensure_user($username, $password, $firstname, $lastname, $email, $courseid, $roleid) {
    global $DB;
    $user = $DB->get_record('user', array('username' => $username, 'deleted' => 0));
    if (!$user) {
        $user = create_user_record($username, $password, 'manual');
        $user->firstname = $firstname;
        $user->lastname = $lastname;
        $user->email = $email;
        $DB->update_record('user', $user);
    }
    $instance = $DB->get_record('enrol', array('courseid' => $courseid, 'enrol' => 'manual'));
    if ($instance && !$DB->record_exists('user_enrolments', array('enrolid' => $instance->id, 'userid' => $user->id))) {
        $plugin = enrol_get_plugin('manual');
        $plugin->enrol_user($instance, $user->id, $roleid);
    }
    return $user;
}

try {
    $teacherrole = $DB->get_field('role', 'id', array('shortname' => 'teacher'));
    $studentrole = $DB->get_field('role', 'id', array('shortname' => 'student'));
    eb_ensure_user(eb_env('DEMO_TEACHER_USER', 'teacher1'), eb_env('DEMO_TEACHER_PASS'),
        '演示', '教师', 'teacher@example.local', $course->id, $teacherrole);
    eb_ensure_user(eb_env('DEMO_STUDENT_USER', 'student1'), eb_env('DEMO_STUDENT_PASS'),
        '演示', '学生', 'student@example.local', $course->id, $studentrole);
    eb_log('演示师生账号已就绪');
} catch (Throwable $e) {
    eb_log('演示账号处理跳过：' . $e->getMessage());
}

// ---------------------------------------------------------------- LTI 1.3 工具类型
$type = $DB->get_record('lti_types', array('clientid' => $clientid));
if (!$type) {
    $type = new stdClass();
    $type->name = 'EduPACS 医学影像病例库';
    $type->state = LTI_TOOL_STATE_CONFIGURED;
    $type->coursevisible = LTI_COURSEVISIBLE_ACTIVITYCHOOSER;
    $type->course = SITEID;
}

$config = new stdClass();
$config->lti_typename = 'EduPACS 医学影像病例库';
$config->lti_description = 'EduPACS 教学病例库与 OHIF 阅片器（LTI 1.3）';
$config->lti_toolurl = $launchurl;
$config->lti_tooldomain = parse_url($launchurl, PHP_URL_HOST);
$config->lti_ltiversion = LTI_VERSION_1P3;
$config->lti_clientid = $clientid;
$config->lti_keytype = LTI_JWK_KEYSET;
$config->lti_publickeyset = $keyseturl;
$config->lti_initiatelogin = $loginurl;
$config->lti_redirectionuris = $launchurl;
$config->lti_contentitem = 1;
$config->lti_toolurl_ContentItemSelectionRequest = $launchurl;
$config->lti_launchcontainer = LTI_LAUNCH_CONTAINER_WINDOW;
$config->lti_acceptgrades = LTI_SETTING_DELEGATE;
$config->ltiservice_gradesynchronization = 1;
$config->lti_sendname = LTI_SETTING_NEVER;
$config->lti_sendemailaddr = LTI_SETTING_NEVER;
$config->lti_coursevisible = LTI_COURSEVISIBLE_ACTIVITYCHOOSER;

try {
    if (!empty($type->id)) {
        lti_update_type($type, $config);
        $typeid = $type->id;
        eb_log('已更新 LTI 1.3 工具 id=' . $typeid);
    } else {
        $typeid = lti_add_type($type, $config);
        eb_log('已创建 LTI 1.3 工具 id=' . $typeid);
    }
} catch (Throwable $e) {
    eb_log('LTI 工具创建失败：' . $e->getMessage());
    exit(1);
}

// Moodle 的 deployment_id 就是工具类型 id
$deploymentid = strval($typeid);

// ---------------------------------------------------------------- 课程内活动
$existing = $DB->get_record_sql(
    "SELECT l.id, cm.id AS cmid FROM {lti} l
       JOIN {course_modules} cm ON cm.instance = l.id
       JOIN {modules} m ON m.id = cm.module AND m.name = 'lti'
      WHERE l.course = ? AND l.typeid = ? LIMIT 1",
    array($course->id, $typeid)
);
if ($existing) {
    $cmid = $existing->cmid;
    eb_log('复用阅片活动 cmid=' . $cmid);
} else {
    $moduleinfo = new stdClass();
    $moduleinfo->course = $course->id;
    $moduleinfo->modulename = 'lti';
    $moduleinfo->name = 'EduPACS 影像阅片（LTI 1.3）';
    $moduleinfo->intro = '点击后在 EduPACS 中浏览教学病例并打开 OHIF 阅片器；作答与批阅可回传 Moodle 成绩簿。';
    $moduleinfo->introformat = FORMAT_HTML;
    $moduleinfo->section = 1;
    $moduleinfo->visible = 1;
    $moduleinfo->typeid = $typeid;
    $moduleinfo->toolurl = $launchurl;
    $moduleinfo->instructorchoiceacceptgrades = LTI_SETTING_DELEGATE;
    $moduleinfo->grade = 100;
    $moduleinfo->launchcontainer = LTI_LAUNCH_CONTAINER_WINDOW;
    $moduleinfo->cmidnumber = 'edupacs-lti-' . $typeid;
    try {
        $cm = add_moduleinfo($moduleinfo, $course);
        $cmid = !empty($cm->coursemodule) ? $cm->coursemodule : $cm->id;
        eb_log('已创建阅片活动 cmid=' . $cmid);
    } catch (Throwable $e) {
        eb_log('阅片活动创建失败：' . $e->getMessage());
        // 工具已注册成功，活动可稍后手工添加；继续输出注册结果
        $cmid = 0;
    }
}

// ---------------------------------------------------------------- 输出结果
$result = array(
    'iss' => rtrim($CFG->wwwroot, '/'),
    'client_id' => $clientid,
    'deployment_id' => $deploymentid,
    'type_id' => $typeid,
    'course_id' => intval($course->id),
    'course_url' => rtrim($CFG->wwwroot, '/') . '/course/view.php?id=' . $course->id,
    'cmid' => intval($cmid),
    'activity_url' => $cmid ? (rtrim($CFG->wwwroot, '/') . '/mod/lti/view.php?id=' . $cmid) : '',
    'created_at' => date('c'),
);
$outdir = $CFG->dataroot . '/edupacs-bootstrap';
if (!is_dir($outdir)) {
    mkdir($outdir, 0777, true);
}
$outfile = $outdir . '/lti-result.json';
file_put_contents($outfile, json_encode($result, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
eb_log('注册结果已写入 ' . $outfile);
eb_log('--- lti-result.json ---');
eb_log(json_encode($result, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
exit(0);
