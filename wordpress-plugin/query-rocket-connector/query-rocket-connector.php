<?php
/**
 * Plugin Name: Query Rocket Connector
 * Description: Lets the GearBoost Query Rocket hub apply approved SEO fixes through Rank Math or Yoast, verify them, and roll them back.
 * Version: 1.1.0
 * Author: GearBoost Marketing
 * Requires PHP: 7.4
 * Requires at least: 6.0
 */

if (!defined('ABSPATH')) {
    exit;
}

define('QR_VERSION', '1.1.0');
define('QR_NS', 'query-rocket/v1');

register_activation_hook(__FILE__, 'qr_activate');

add_action('plugins_loaded', function () {
    if (get_option('qr_db_version') !== QR_VERSION) {
        qr_activate();
    }
});

function qr_activate() {
    global $wpdb;
    $table = $wpdb->prefix . 'qr_changes';
    $charset = $wpdb->get_charset_collate();
    require_once ABSPATH . 'wp-admin/includes/upgrade.php';
    dbDelta("CREATE TABLE $table (
        id bigint(20) unsigned NOT NULL AUTO_INCREMENT,
        fix_id varchar(40) NOT NULL DEFAULT '',
        object_ref varchar(60) NOT NULL DEFAULT '',
        kind varchar(40) NOT NULL DEFAULT '',
        target text NULL,
        old_value longtext NULL,
        new_value longtext NULL,
        created_at datetime NOT NULL,
        rolled_back tinyint(1) NOT NULL DEFAULT 0,
        PRIMARY KEY  (id),
        KEY fix_id (fix_id)
    ) $charset;");
    if (!get_option('qr_secret')) {
        update_option('qr_secret', wp_generate_password(48, false, false), false);
    }
    update_option('qr_db_version', QR_VERSION, false);
}

/* ------------------------------------------------------------------ auth */

function qr_verify_request(WP_REST_Request $request) {
    $secret = get_option('qr_secret');
    $ts = $request->get_header('x-qr-timestamp');
    $sig = $request->get_header('x-qr-signature');
    if (!$secret || !$ts || !$sig) {
        return new WP_Error('qr_unauthorized', 'Missing signature.', array('status' => 401));
    }
    if (abs(time() - intval($ts)) > 300) {
        return new WP_Error('qr_clock', 'Request expired. Check the server clocks.', array('status' => 401));
    }
    $payload = $ts . "\n" . strtoupper($request->get_method()) . "\n" . $request->get_route() . "\n" . hash('sha256', $request->get_body());
    $expected = hash_hmac('sha256', $payload, $secret);
    if (!hash_equals($expected, $sig)) {
        return new WP_Error('qr_unauthorized', 'Bad signature.', array('status' => 401));
    }
    return true;
}

/* ------------------------------------------------------------------ SEO plugin fields */

function qr_seo_plugin() {
    if (defined('RANK_MATH_VERSION') || class_exists('RankMath')) {
        return 'rankmath';
    }
    if (defined('WPSEO_VERSION')) {
        return 'yoast';
    }
    return 'none';
}

function qr_keys($field) {
    $map = array(
        'rankmath' => array('title' => 'rank_math_title', 'description' => 'rank_math_description', 'focus' => 'rank_math_focus_keyword'),
        'yoast'    => array('title' => '_yoast_wpseo_title', 'description' => '_yoast_wpseo_metadesc', 'focus' => '_yoast_wpseo_focuskw'),
        'none'     => array('title' => '_qr_title', 'description' => '_qr_description', 'focus' => '_qr_focus_keyword'),
    );
    return $map[qr_seo_plugin()][$field];
}

function qr_front_option() {
    $p = qr_seo_plugin();
    if ($p === 'rankmath') {
        return array('rank-math-options-titles', array('title' => 'homepage_title', 'description' => 'homepage_description'));
    }
    if ($p === 'yoast') {
        return array('wpseo_titles', array('title' => 'title-home-wpseo', 'description' => 'metadesc-home-wpseo'));
    }
    return array('qr_front_fields', array('title' => 'title', 'description' => 'description', 'focus' => 'focus'));
}

/** Map a public URL to "post:ID", "term:ID", or "front:posts". */
function qr_resolve($url) {
    $url = strtok($url, '?#');
    if (untrailingslashit($url) === untrailingslashit(home_url('/'))) {
        if (get_option('show_on_front') === 'page' && get_option('page_on_front')) {
            return 'post:' . intval(get_option('page_on_front'));
        }
        return 'front:posts';
    }
    $id = url_to_postid($url);
    if ($id) {
        return 'post:' . $id;
    }
    $path = trim(wp_parse_url($url, PHP_URL_PATH), '/');
    $slug = basename($path);
    foreach (get_taxonomies(array('public' => true)) as $tax) {
        $term = get_term_by('slug', $slug, $tax);
        if ($term && !is_wp_error($term) && untrailingslashit(get_term_link($term)) === untrailingslashit($url)) {
            return 'term:' . $term->term_id;
        }
    }
    return '';
}

function qr_get_field($ref, $field) {
    list($type, $id) = array_pad(explode(':', $ref, 2), 2, '');
    if ($type === 'post') {
        return (string) get_post_meta(intval($id), qr_keys($field), true);
    }
    if ($type === 'term') {
        return (string) get_term_meta(intval($id), qr_keys($field), true);
    }
    list($opt, $keys) = qr_front_option();
    $o = get_option($opt, array());
    return isset($keys[$field], $o[$keys[$field]]) ? (string) $o[$keys[$field]] : '';
}

function qr_set_field($ref, $field, $value) {
    list($type, $id) = array_pad(explode(':', $ref, 2), 2, '');
    if ($type === 'post') {
        update_post_meta(intval($id), qr_keys($field), $value);
        return true;
    }
    if ($type === 'term') {
        if (qr_seo_plugin() === 'yoast') {
            return new WP_Error('qr_unsupported', 'Yoast term fields are not supported yet.', array('status' => 422));
        }
        update_term_meta(intval($id), qr_keys($field), $value);
        return true;
    }
    list($opt, $keys) = qr_front_option();
    if (!isset($keys[$field])) {
        return new WP_Error('qr_unsupported', 'That field can\'t be set on a blog-posts homepage.', array('status' => 422));
    }
    $o = get_option($opt, array());
    $o[$keys[$field]] = $value;
    update_option($opt, $o);
    return true;
}

function qr_modified($ref) {
    list($type, $id) = array_pad(explode(':', $ref, 2), 2, '');
    if ($type === 'post') {
        $p = get_post(intval($id));
        return $p ? $p->post_modified_gmt : '';
    }
    return '';
}

/* ------------------------------------------------------------------ change log */

function qr_log($fix_id, $ref, $kind, $target, $old, $new) {
    global $wpdb;
    $wpdb->insert($wpdb->prefix . 'qr_changes', array(
        'fix_id' => (string) $fix_id, 'object_ref' => $ref, 'kind' => $kind, 'target' => $target,
        'old_value' => maybe_serialize($old), 'new_value' => maybe_serialize($new), 'created_at' => current_time('mysql', true),
    ));
    return (int) $wpdb->insert_id;
}

function qr_existing_change($fix_id) {
    global $wpdb;
    return $wpdb->get_row($wpdb->prepare(
        "SELECT * FROM {$wpdb->prefix}qr_changes WHERE fix_id = %s AND rolled_back = 0 AND kind <> 'builder' ORDER BY id DESC LIMIT 1", (string) $fix_id));
}

function qr_purge($ref) {
    list($type, $id) = array_pad(explode(':', $ref, 2), 2, '');
    if ($type === 'post') {
        clean_post_cache(intval($id));
        if (function_exists('rocket_clean_post')) {
            rocket_clean_post(intval($id));
        }
        do_action('litespeed_purge_post', intval($id));
    }
    do_action('breeze_clear_all_cache');   // Cloudways (Breeze + Varnish)
    if (function_exists('w3tc_flush_all')) {
        w3tc_flush_all();
    }
    if (function_exists('wp_cache_clear_cache')) {
        wp_cache_clear_cache();
    }
}

/* ------------------------------------------------------------------ images */

function qr_normalize_src($src) {
    $src = strtok($src, '?#');
    return preg_replace('/-\d+x\d+(?=\.[a-z0-9]+$)/i', '', $src);
}

function qr_alt_overrides() {
    $o = get_option('qr_alt_overrides', array());
    return is_array($o) ? $o : array();
}

function qr_apply_alt($src, $alt) {
    $key = qr_normalize_src($src);
    $overrides = qr_alt_overrides();
    $old = isset($overrides[$key]) ? $overrides[$key] : '';
    $attachment = attachment_url_to_postid($key);
    if ($attachment) {
        $old = (string) get_post_meta($attachment, '_wp_attachment_image_alt', true);
        update_post_meta($attachment, '_wp_attachment_image_alt', $alt);
    }
    if ($alt === '') {
        unset($overrides[$key]);
    } else {
        $overrides[$key] = $alt;
    }
    update_option('qr_alt_overrides', $overrides, true);
    return $old;
}

/** Fill empty alt attributes in rendered content from the override map. */
function qr_fill_alts($html) {
    $overrides = qr_alt_overrides();
    if (!$overrides || stripos($html, '<img') === false) {
        return $html;
    }
    return preg_replace_callback('/<img\b[^>]*>/i', function ($m) use ($overrides) {
        $tag = $m[0];
        if (!preg_match('/\ssrc=["\']([^"\']+)["\']/i', $tag, $s)) {
            return $tag;
        }
        $key = qr_normalize_src(html_entity_decode($s[1]));
        if (!isset($overrides[$key])) {
            return $tag;
        }
        $alt = esc_attr($overrides[$key]);
        if (preg_match('/\salt=(["\'])\s*\1/i', $tag)) {
            return preg_replace('/\salt=(["\'])\s*\1/i', ' alt="' . $alt . '"', $tag, 1);
        }
        if (!preg_match('/\salt=/i', $tag)) {
            return preg_replace('/^<img\b/i', '<img alt="' . $alt . '"', $tag, 1);
        }
        return $tag;
    }, $html);
}
add_filter('the_content', 'qr_fill_alts', 99);
add_filter('post_thumbnail_html', 'qr_fill_alts', 99);
add_filter('wp_get_attachment_image_attributes', function ($attr) {
    if (empty($attr['alt']) && !empty($attr['src'])) {
        $o = qr_alt_overrides();
        $k = qr_normalize_src($attr['src']);
        if (isset($o[$k])) {
            $attr['alt'] = $o[$k];
        }
    }
    return $attr;
}, 99);

/* ------------------------------------------------------------------ schema + no-SEO-plugin output */

add_action('wp_head', function () {
    $ref = '';
    if (is_front_page() && !is_page()) {
        $ref = 'front:posts';
        $json = get_option('qr_front_schema', '');
    } elseif (is_singular()) {
        $json = get_post_meta(get_queried_object_id(), '_qr_schema', true);
    } else {
        $json = '';
    }
    if ($json && json_decode($json) !== null) {
        echo "\n<script type=\"application/ld+json\" class=\"query-rocket-schema\">" . wp_json_encode(json_decode($json)) . "</script>\n";
    }
    if (qr_seo_plugin() === 'none' && is_singular()) {
        $d = get_post_meta(get_queried_object_id(), '_qr_description', true);
        if ($d) {
            echo '<meta name="description" content="' . esc_attr($d) . "\">\n";
        }
    }
}, 20);

add_filter('pre_get_document_title', function ($title) {
    if (qr_seo_plugin() === 'none' && is_singular()) {
        $t = get_post_meta(get_queried_object_id(), '_qr_title', true);
        if ($t) {
            return $t;
        }
    }
    return $title;
}, 99);

function qr_get_schema($ref) {
    list($type, $id) = array_pad(explode(':', $ref, 2), 2, '');
    return $type === 'post' ? (string) get_post_meta(intval($id), '_qr_schema', true) : (string) get_option('qr_front_schema', '');
}

function qr_set_schema($ref, $json) {
    list($type, $id) = array_pad(explode(':', $ref, 2), 2, '');
    if ($type === 'post') {
        $json === '' ? delete_post_meta(intval($id), '_qr_schema') : update_post_meta(intval($id), '_qr_schema', wp_slash($json));
    } elseif ($type === 'front') {
        update_option('qr_front_schema', $json, true);
    } else {
        return new WP_Error('qr_unsupported', 'Schema can only be added to posts, pages, and the homepage.', array('status' => 422));
    }
    return true;
}

/* ------------------------------------------------------------------ links */

function qr_replace_link($post_id, $old_url, $new_url) {
    $post = get_post($post_id);
    if (!$post) {
        return new WP_Error('qr_missing', 'Post not found.', array('status' => 404));
    }
    $content = $post->post_content;
    $path = wp_parse_url($old_url, PHP_URL_PATH);
    $candidates = array_unique(array_filter(array($old_url, untrailingslashit($old_url), $path, untrailingslashit((string) $path))));
    $count = 0;
    foreach ($candidates as $c) {
        $q = preg_quote($c, '/');
        if ($new_url === 'REMOVE') {
            $content = preg_replace('/<a\b[^>]*\shref=(["\'])' . $q . '\/?\1[^>]*>(.*?)<\/a>/is', '$2', $content, -1, $n);
        } else {
            $content = preg_replace('/(\shref=)(["\'])' . $q . '\/?\2/i', '$1$2' . str_replace('$', '\$', esc_url($new_url)) . '$2', $content, -1, $n);
        }
        $count += $n;
    }
    if (!$count) {
        return new WP_Error('qr_not_found', 'The link isn\'t in this page\'s editable content. It may be in a menu, footer, widget, or page builder data.', array('status' => 422));
    }
    $old = $post->post_content;
    $r = wp_update_post(array('ID' => $post_id, 'post_content' => wp_slash($content)), true);
    return is_wp_error($r) ? $r : $old;
}


/* ------------------------------------------------------------------ page builder data (Oxygen 6 / Breakdance) */
/*
 * Off unless BOTH switches are on: "Allow page builder edits" in Settings > Query Rocket here, and the matching
 * setting in the Query Rocket hub. Every edit backs up the page's entire builder data first, so rollback restores it exactly.
 */

function qr_builder_allowed() {
    return (bool) get_option('qr_allow_builder', false);
}

function qr_builder_meta_keys() {
    return apply_filters('qr_builder_meta_keys', array('_oxygen_data', '_breakdance_data', 'ct_builder_json'));
}

/** Decode JSON that may itself contain JSON-encoded strings (Breakdance nests the tree this way). */
function qr_decode_layers($value, &$layers) {
    if (is_string($value)) {
        $t = ltrim($value);
        if ($t !== '' && ($t[0] === '{' || $t[0] === '[')) {
            $d = json_decode($value, true);
            if (json_last_error() === JSON_ERROR_NONE && is_array($d)) {
                $layers++;
                // Remember how this layer was written so saving it back changes only what we edited.
                return array('__qr_json' => qr_decode_layers($d, $layers),
                    '__qr_fmt' => array('slashes' => strpos($value, '\\/') !== false, 'unicode' => (bool) preg_match('/\\\\u[0-9a-fA-F]{4}/', $value)));
            }
        }
        return $value;
    }
    if (is_array($value)) {
        foreach ($value as $k => $v) {
            $value[$k] = qr_decode_layers($v, $layers);
        }
    }
    return $value;
}

function qr_encode_layers($value) {
    if (is_array($value)) {
        if (array_key_exists('__qr_json', $value) && array_key_exists('__qr_fmt', $value) && count($value) === 2) {
            $flags = ($value['__qr_fmt']['slashes'] ? 0 : JSON_UNESCAPED_SLASHES) | ($value['__qr_fmt']['unicode'] ? 0 : JSON_UNESCAPED_UNICODE);
            return wp_json_encode(qr_encode_layers($value['__qr_json']), $flags);
        }
        foreach ($value as $k => $v) {
            $value[$k] = qr_encode_layers($v);
        }
    }
    return $value;
}

function qr_builder_data($post_id) {
    foreach (qr_builder_meta_keys() as $key) {
        $raw = get_post_meta($post_id, $key, true);
        if ($raw === '' || $raw === null || $raw === false) {
            continue;
        }
        $layers = 0;
        $tree = qr_decode_layers($raw, $layers);
        if ($layers > 0 || is_array($raw)) {
            return array('key' => $key, 'raw' => $raw, 'tree' => $tree);
        }
    }
    return null;
}

function qr_same_image($a, $b) {
    $a = qr_normalize_src((string) $a);
    $b = qr_normalize_src((string) $b);
    if ($a === '' || $b === '') {
        return false;
    }
    return $a === $b || basename($a) === basename($b);
}

/** Set alt on every builder element whose image URL matches. Returns the number changed. */
function qr_tree_set_alt(&$node, $src, $alt) {
    $n = 0;
    if (!is_array($node)) {
        return 0;
    }
    $has_image = false;
    foreach ($node as $k => $v) {
        if (is_string($v) && in_array(strtolower((string) $k), array('url', 'src', 'image', 'image_url'), true) && qr_same_image($v, $src)) {
            $has_image = true;
        }
    }
    if ($has_image) {
        $node['alt'] = $alt;
        $n++;
    }
    foreach ($node as $k => &$child) {
        if (is_array($child)) {
            $n += qr_tree_set_alt($child, $src, $alt);
        }
    }
    unset($child);
    return $n;
}

/** Replace a link URL in link-like fields. Returns the number changed. */
function qr_tree_replace_link(&$node, $candidates, $new_url) {
    $n = 0;
    if (!is_array($node)) {
        return 0;
    }
    foreach ($node as $k => &$v) {
        if (is_array($v)) {
            $n += qr_tree_replace_link($v, $candidates, $new_url);
        } elseif (is_string($v) && in_array(strtolower((string) $k), array('url', 'href', 'link', 'link_url'), true)) {
            foreach ($candidates as $c) {
                if (untrailingslashit($v) === untrailingslashit($c)) {
                    $v = $new_url;
                    $n++;
                    break;
                }
            }
        }
    }
    unset($v);
    return $n;
}

/** Apply an edit to builder data. $dry_run reports what would change without saving. */
function qr_builder_edit($post_id, $kind, $target, $value, $dry_run = false) {
    if (!qr_builder_allowed()) {
        return new WP_Error('qr_builder_off', 'Page builder edits are turned off on this site (Settings > Query Rocket).', array('status' => 422));
    }
    $data = qr_builder_data($post_id);
    if (!$data) {
        return new WP_Error('qr_no_builder', 'This page has no page builder data Query Rocket recognizes.', array('status' => 422));
    }
    $tree = $data['tree'];
    if ($kind === 'image_alt') {
        $count = qr_tree_set_alt($tree, $target, sanitize_text_field($value));
    } else {
        if ($value === 'REMOVE') {
            return new WP_Error('qr_builder_remove', 'Removing a link inside page builder data isn\'t supported. Pick a replacement page instead.', array('status' => 422));
        }
        $path = wp_parse_url($target, PHP_URL_PATH);
        $count = qr_tree_replace_link($tree, array_filter(array($target, $path)), esc_url_raw($value));
    }
    if (!$count) {
        return new WP_Error('qr_builder_not_found', 'Couldn\'t find that ' . ($kind === 'image_alt' ? 'image' : 'link') . ' in the page builder data.', array('status' => 422));
    }
    if ($dry_run) {
        return array('changes' => $count, 'meta_key' => $data['key']);
    }
    $encoded = qr_encode_layers($tree);
    update_post_meta($post_id, $data['key'], wp_slash($encoded));
    return array('changes' => $count, 'meta_key' => $data['key'], 'old' => $data['raw']);
}

/* ------------------------------------------------------------------ REST */

add_action('rest_api_init', function () {
    $auth = array('permission_callback' => 'qr_verify_request');
    register_rest_route(QR_NS, '/status', $auth + array('methods' => 'GET', 'callback' => 'qr_rest_status'));
    register_rest_route(QR_NS, '/pages', $auth + array('methods' => 'POST', 'callback' => 'qr_rest_pages'));
    register_rest_route(QR_NS, '/apply', $auth + array('methods' => 'POST', 'callback' => 'qr_rest_apply'));
    register_rest_route(QR_NS, '/rollback', $auth + array('methods' => 'POST', 'callback' => 'qr_rest_rollback'));
});

function qr_rest_status() {
    return array(
        'ok' => true, 'plugin_version' => QR_VERSION, 'wp_version' => get_bloginfo('version'),
        'seo_plugin' => qr_seo_plugin(), 'home' => home_url('/'), 'time' => time(),
        'builder_allowed' => qr_builder_allowed(),
        'builder' => defined('OXYGEN_VERSION') || defined('__BREAKDANCE_VERSION') || function_exists('oxygen_vsb_register_condition') ? 'oxygen' : '',
    );
}

function qr_page_info($url) {
    $ref = qr_resolve($url);
    if (!$ref) {
        return array('url' => $url, 'object' => '', 'supported' => false);
    }
    return array(
        'url' => $url, 'object' => $ref, 'supported' => true, 'modified_gmt' => qr_modified($ref),
        'title_raw' => qr_get_field($ref, 'title'), 'description_raw' => qr_get_field($ref, 'description'),
        'focus_keyword' => strpos($ref, 'front:') === 0 ? '' : qr_get_field($ref, 'focus'),
    );
}

function qr_rest_pages(WP_REST_Request $request) {
    $urls = (array) $request->get_param('urls');
    $out = array();
    foreach (array_slice($urls, 0, 500) as $u) {
        $out[] = qr_page_info(esc_url_raw($u));
    }
    return array('seo_plugin' => qr_seo_plugin(), 'pages' => $out);
}

function qr_rest_apply(WP_REST_Request $request) {
    $fix_id = sanitize_text_field((string) $request->get_param('fix_id'));
    $kind = sanitize_key((string) $request->get_param('kind'));
    $url = esc_url_raw((string) $request->get_param('url'));
    $value = (string) $request->get_param('value');
    $target = (string) $request->get_param('target');
    $expected = $request->get_param('expected_old');

    $done = qr_existing_change($fix_id);
    if ($done) {
        return array('ok' => true, 'change_id' => (int) $done->id, 'already_applied' => true);
    }
    $ref = qr_resolve($url);
    if (!$ref) {
        return new WP_Error('qr_unresolved', 'Couldn\'t match this URL to a post, page, or term.', array('status' => 404));
    }

    $fields = array('title' => 'title', 'meta_description' => 'description', 'focus_keyword' => 'focus');
    if (isset($fields[$kind])) {
        $field = $fields[$kind];
        $value = trim(wp_strip_all_tags($value));
        $current = qr_get_field($ref, $field);
        if ($expected !== null && (string) $expected !== $current) {
            return new WP_Error('qr_conflict', 'This field changed since Query Rocket last read it. Someone may have edited it.', array('status' => 409, 'current' => $current));
        }
        $r = qr_set_field($ref, $field, $value);
        if (is_wp_error($r)) {
            return $r;
        }
        $id = qr_log($fix_id, $ref, $kind, '', $current, $value);
    } elseif ($kind === 'image_alt') {
        $alt = sanitize_text_field($value);
        $use_builder = $request->get_param('allow_builder') && qr_builder_allowed() && strpos($ref, 'post:') === 0;
        if ($use_builder && qr_builder_data(intval(substr($ref, 5)))) {
            $b = qr_builder_edit(intval(substr($ref, 5)), 'image_alt', $target, $alt);
            if (!is_wp_error($b)) {
                qr_log($fix_id, $ref, 'builder', $b['meta_key'], $b['old'], '');
            }
        }
        $old = qr_apply_alt($target, $alt);
        $id = qr_log($fix_id, $ref, $kind, $target, $old, $alt);
    } elseif ($kind === 'schema_local_business') {
        if (json_decode($value) === null) {
            return new WP_Error('qr_bad_json', 'Schema is not valid JSON.', array('status' => 422));
        }
        $old = qr_get_schema($ref);
        $r = qr_set_schema($ref, $value);
        if (is_wp_error($r)) {
            return $r;
        }
        $id = qr_log($fix_id, $ref, $kind, '', $old, $value);
    } elseif ($kind === 'broken_link') {
        if (strpos($ref, 'post:') !== 0) {
            return new WP_Error('qr_unsupported', 'Links can only be edited inside posts and pages.', array('status' => 422));
        }
        $post_id = intval(substr($ref, 5));
        $old = qr_replace_link($post_id, $target, $value === 'REMOVE' ? 'REMOVE' : esc_url_raw($value));
        if (is_wp_error($old) && $old->get_error_code() === 'qr_not_found' && $request->get_param('allow_builder')) {
            $b = qr_builder_edit($post_id, 'broken_link', $target, $value);
            if (is_wp_error($b)) {
                return $b;
            }
            $id = qr_log($fix_id, $ref, 'builder', $b['meta_key'], $b['old'], '');
        } elseif (is_wp_error($old)) {
            return $old;
        } else {
            $id = qr_log($fix_id, $ref, $kind, $target, $old, $value);
        }
    } else {
        return new WP_Error('qr_kind', 'Unknown fix type.', array('status' => 400));
    }
    qr_purge($ref);
    return array('ok' => true, 'change_id' => $id, 'object' => $ref);
}

function qr_rest_rollback(WP_REST_Request $request) {
    global $wpdb;
    $id = intval($request->get_param('change_id'));
    $row = $wpdb->get_row($wpdb->prepare("SELECT * FROM {$wpdb->prefix}qr_changes WHERE id = %d", $id));
    if (!$row) {
        return new WP_Error('qr_missing', 'Change not found.', array('status' => 404));
    }
    if ($row->rolled_back) {
        return array('ok' => true, 'already_rolled_back' => true);
    }
    $row->old_value = maybe_unserialize($row->old_value);
    $fields = array('title' => 'title', 'meta_description' => 'description', 'focus_keyword' => 'focus');
    if (isset($fields[$row->kind])) {
        $r = qr_set_field($row->object_ref, $fields[$row->kind], $row->old_value);
    } elseif ($row->kind === 'image_alt') {
        qr_apply_alt($row->target, (string) $row->old_value);
        $r = true;
    } elseif ($row->kind === 'schema_local_business') {
        $r = qr_set_schema($row->object_ref, (string) $row->old_value);
    } elseif ($row->kind === 'builder') {
        update_post_meta(intval(substr($row->object_ref, 5)), $row->target, wp_slash(maybe_unserialize($row->old_value)));
        $r = true;
    } elseif ($row->kind === 'broken_link') {
        $r = wp_update_post(array('ID' => intval(substr($row->object_ref, 5)), 'post_content' => wp_slash($row->old_value)), true);
    } else {
        $r = new WP_Error('qr_kind', 'Unknown change type.', array('status' => 400));
    }
    if (is_wp_error($r)) {
        return $r;
    }
    $wpdb->update($wpdb->prefix . 'qr_changes', array('rolled_back' => 1), array('id' => $id));
    // Image alt fixes may also have edited builder data under the same fix: restore that too.
    $siblings = $wpdb->get_results($wpdb->prepare(
        "SELECT * FROM {$wpdb->prefix}qr_changes WHERE fix_id = %s AND kind = 'builder' AND rolled_back = 0 AND id <> %d", $row->fix_id, $id));
    foreach ($siblings as $sib) {
        update_post_meta(intval(substr($sib->object_ref, 5)), $sib->target, wp_slash(maybe_unserialize($sib->old_value)));
        $wpdb->update($wpdb->prefix . 'qr_changes', array('rolled_back' => 1), array('id' => $sib->id));
    }
    qr_purge($row->object_ref);
    return array('ok' => true);
}

/* ------------------------------------------------------------------ admin screen */

add_action('admin_menu', function () {
    add_options_page('Query Rocket', 'Query Rocket', 'manage_options', 'query-rocket', 'qr_admin_page');
});

add_action('admin_post_qr_builder', function () {
    if (!current_user_can('manage_options') || !check_admin_referer('qr_builder')) {
        wp_die('Not allowed.');
    }
    update_option('qr_allow_builder', !empty($_POST['allow_builder']), false);
    wp_safe_redirect(admin_url('options-general.php?page=query-rocket&saved=1'));
    exit;
});

add_action('admin_post_qr_regenerate', function () {
    if (!current_user_can('manage_options') || !check_admin_referer('qr_regenerate')) {
        wp_die('Not allowed.');
    }
    update_option('qr_secret', wp_generate_password(48, false, false), false);
    wp_safe_redirect(admin_url('options-general.php?page=query-rocket&regenerated=1'));
    exit;
});

function qr_admin_page() {
    global $wpdb;
    if (!current_user_can('manage_options')) {
        return;
    }
    $secret = get_option('qr_secret');
    $rows = $wpdb->get_results("SELECT * FROM {$wpdb->prefix}qr_changes ORDER BY id DESC LIMIT 50");
    $labels = array('rankmath' => 'Rank Math', 'yoast' => 'Yoast', 'none' => 'None (Query Rocket manages titles and descriptions)');
    ?>
    <div class="wrap">
        <h1>Query Rocket Connector</h1>
        <?php if (isset($_GET['regenerated'])) : ?><div class="notice notice-success"><p>New key created. Paste it into Query Rocket; the old key no longer works.</p></div><?php endif; ?>
        <p>Paste this site address and connection key into the site's <strong>WordPress connection</strong> screen in Query Rocket.</p>
        <table class="form-table" role="presentation">
            <tr><th>Site address</th><td><code><?php echo esc_html(home_url('/')); ?></code></td></tr>
            <tr><th>Connection key</th><td><input type="text" readonly class="regular-text code" style="width:32em" value="<?php echo esc_attr($secret); ?>" onclick="this.select()"></td></tr>
            <tr><th>SEO plugin</th><td><?php echo esc_html($labels[qr_seo_plugin()]); ?></td></tr>
        </table>
        <form method="post" action="<?php echo esc_url(admin_url('admin-post.php')); ?>" onsubmit="return confirm('Create a new key? Query Rocket will stop working on this site until you paste the new key in.');">
            <input type="hidden" name="action" value="qr_regenerate"><?php wp_nonce_field('qr_regenerate'); ?>
            <?php submit_button('Create a new key', 'secondary', 'submit', false); ?>
        </form>
        <h2 style="margin-top:2em">Page builder edits</h2>
        <form method="post" action="<?php echo esc_url(admin_url('admin-post.php')); ?>">
            <input type="hidden" name="action" value="qr_builder"><?php wp_nonce_field('qr_builder'); ?>
            <label><input type="checkbox" name="allow_builder" value="1" <?php checked(qr_builder_allowed()); ?>>
            Allow Query Rocket to edit image alt text and links inside Oxygen page builder data</label>
            <p class="description">Also needs to be turned on for this site in Query Rocket. The page's full builder data is backed up before every edit, and rollback restores it exactly.</p>
            <?php submit_button('Save', 'secondary', 'submit', false); ?>
        </form>
        <h2 style="margin-top:2em">Recent changes</h2>
        <p>Every change Query Rocket makes is logged here with the previous value. Roll back from the Query Rocket dashboard.</p>
        <table class="widefat striped">
            <thead><tr><th>When (UTC)</th><th>Type</th><th>Where</th><th>Before</th><th>After</th><th>Status</th></tr></thead>
            <tbody>
            <?php if (!$rows) : ?><tr><td colspan="6">No changes yet.</td></tr><?php endif; ?>
            <?php foreach ($rows as $r) : ?>
                <tr>
                    <td><?php echo esc_html($r->created_at); ?></td>
                    <td><?php echo esc_html($r->kind); ?></td>
                    <td><?php echo esc_html($r->object_ref . ($r->target ? ' ' . $r->target : '')); ?></td>
                    <td><?php echo $r->kind === 'builder' ? 'Page builder data (backed up)' : esc_html(wp_trim_words(wp_strip_all_tags((string) maybe_unserialize($r->old_value)), 14)); ?></td>
                    <td><?php echo $r->kind === 'builder' ? '' : esc_html(wp_trim_words(wp_strip_all_tags((string) maybe_unserialize($r->new_value)), 14)); ?></td>
                    <td><?php echo $r->rolled_back ? 'Rolled back' : 'Live'; ?></td>
                </tr>
            <?php endforeach; ?>
            </tbody>
        </table>
    </div>
    <?php
}
