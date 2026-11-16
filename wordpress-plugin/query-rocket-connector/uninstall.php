<?php
// Removes Query Rocket's settings. The change log table is kept so history isn't lost by accident.
if (!defined('WP_UNINSTALL_PLUGIN')) {
    exit;
}
delete_option('qr_secret');
