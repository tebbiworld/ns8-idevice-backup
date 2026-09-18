<!--
  Copyright (C) 2026 tebbi
  SPDX-License-Identifier: GPL-3.0-or-later
-->
<template>
  <cv-grid fullWidth>
    <cv-row>
      <cv-column class="page-title"><h2>{{ $t("settings.title") }}</h2></cv-column>
    </cv-row>
    <cv-row v-if="error.getConfiguration">
      <cv-column>
        <NsInlineNotification kind="error" :title="$t('action.get-configuration')" :description="error.getConfiguration" :showCloseButton="false" />
      </cv-column>
    </cv-row>

    <!-- Engine state + how to pair -->
    <cv-row>
      <cv-column>
        <cv-tile light>
          <NsInlineNotification
            v-if="!loading.getConfiguration"
            :kind="container_running ? 'success' : 'info'"
            :title="container_running ? $t('settings.engine_running') : $t('settings.engine_stopped')"
            :showCloseButton="false"
            class="info-tile"
          />
          <h4 class="section-first">{{ $t("settings.pairing_title") }}</h4>
          <ol class="steps">
            <li>{{ $t("settings.pairing_step1") }}</li>
            <li>{{ $t("settings.pairing_step2") }}</li>
            <li>{{ $t("settings.pairing_step3") }}</li>
          </ol>
          <NsInlineNotification kind="warning" :title="$t('settings.wifi_warn_title')" :description="$t('settings.wifi_warn')" :showCloseButton="false" class="info-tile" />
        </cv-tile>
      </cv-column>
    </cv-row>

    <!-- Global options -->
    <cv-row>
      <cv-column>
        <cv-tile light>
          <h4 class="section-first">{{ $t("settings.options_title") }}</h4>
          <cv-form @submit.prevent="saveOptions">
            <cv-number-input :label="$t('settings.retention')" v-model="retention" :min="1" :max="100" :helper-text="$t('settings.retention_helper')" :disabled="busy" class="field"></cv-number-input>
            <cv-toggle value="includeBackups" :label="$t('settings.include_backups')" v-model="include_backups" :disabled="busy" class="field">
              <template slot="text-left">{{ $t("settings.off") }}</template>
              <template slot="text-right">{{ $t("settings.on") }}</template>
            </cv-toggle>
            <div class="bx--form__helper-text">{{ $t("settings.include_backups_helper") }}</div>
            <NsButton kind="primary" :icon="Save20" :loading="loading.configureModule" :disabled="busy" class="field">{{ $t("settings.save") }}</NsButton>
          </cv-form>
        </cv-tile>
      </cv-column>
    </cv-row>

    <!-- Add / update a device -->
    <cv-row>
      <cv-column>
        <cv-tile light>
          <h4 class="section-first">{{ $t("settings.add_title") }}</h4>
          <cv-text-input :label="$t('settings.device_name')" v-model.trim="upload.name" :placeholder="$t('settings.device_name_placeholder')" :disabled="uploadBusy" class="field"></cv-text-input>
          <cv-text-input :label="$t('settings.device_ip')" v-model.trim="upload.ip" :placeholder="$t('settings.device_ip_placeholder')" :helper-text="$t('settings.device_ip_helper')" :disabled="uploadBusy" :invalid-message="$t(error.upload_ip)" class="field"></cv-text-input>
          <cv-text-input type="password" :label="$t('settings.enc_password')" v-model="upload.encryption_password" :helper-text="$t('settings.enc_password_helper')" :password-hide-label="$t('settings.hide')" :password-show-label="$t('settings.show')" :disabled="uploadBusy" class="field"></cv-text-input>
          <div class="field">
            <div class="bx--label">{{ $t("settings.pairing_file") }}</div>
            <input type="file" ref="pairingFile" @change="onFileChange" :disabled="uploadBusy" accept=".plist,.mobiledevicepairing,application/xml,text/xml" />
            <div class="bx--form__helper-text">{{ $t("settings.pairing_file_helper") }}</div>
          </div>
          <NsInlineNotification v-if="error.uploadPairing" kind="error" :title="$t('action.upload-pairing')" :description="error.uploadPairing" :showCloseButton="false" class="info-tile" />
          <NsButton kind="primary" :icon="Upload20" :loading="loading.uploadPairing" :disabled="uploadBusy || !upload.content" @click="uploadPairing">{{ $t("settings.upload") }}</NsButton>
        </cv-tile>
      </cv-column>
    </cv-row>

    <!-- Devices -->
    <cv-row>
      <cv-column>
        <cv-tile light>
          <h4 class="section-first">{{ $t("settings.devices_title") }}</h4>
          <NsInlineNotification v-if="error.runBackup" kind="error" :title="$t('action.run-backup')" :description="error.runBackup" :showCloseButton="false" class="info-tile" />
          <p v-if="!devices.length" class="bx--form__helper-text">{{ $t("settings.no_devices") }}</p>
          <table v-else class="devices">
            <thead>
              <tr>
                <th>{{ $t("settings.col_name") }}</th>
                <th>{{ $t("settings.col_ip") }}</th>
                <th>{{ $t("settings.col_pairing") }}</th>
                <th>{{ $t("settings.col_encryption") }}</th>
                <th>{{ $t("settings.col_mode") }}</th>
                <th>{{ $t("settings.col_last_backup") }}</th>
                <th>{{ $t("settings.col_count") }}</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="d in devices" :key="d.udid">
                <td>{{ d.name }}<div class="udid">{{ d.udid }}</div></td>
                <td>{{ d.ip || "—" }}</td>
                <td><span :class="d.pairing_present ? 'ok' : 'bad'">{{ d.pairing_present ? "✓" : "✗" }}</span></td>
                <td>{{ d.encryption || d.encryption_password_set ? "✓" : "—" }}</td>
                <td>
                  <select class="mode-select" :value="d.backup_mode || 'full'" :disabled="!!loading.mode" @change="updateMode(d.udid, $event.target.value)">
                    <option value="full">{{ $t("settings.mode_full") }}</option>
                    <option value="incremental">{{ $t("settings.mode_incremental") }}</option>
                  </select>
                </td>
                <td>
                  <span v-if="d.last_backup">{{ formatTime(d.last_backup) }}</span>
                  <span v-else>—</span>
                  <div v-if="d.last_status" class="status" :class="d.last_status === 'ok' ? 'ok' : 'bad'">{{ d.last_status }}</div>
                </td>
                <td>{{ d.backup_count }}</td>
                <td class="actions">
                  <NsButton kind="secondary" size="small" :icon="Backup16" :loading="loading.backup === d.udid" :disabled="!!loading.backup || !container_running || !d.pairing_present || !d.ip" @click="runBackup(d.udid)">{{ $t("settings.backup_now") }}</NsButton>
                  <NsButton kind="tertiary" size="small" :icon="Restore16" :disabled="!!loading.backup || !d.backup_count" @click="openRestore(d)">{{ $t("settings.restore") }}</NsButton>
                  <NsButton kind="danger--ghost" size="small" :icon="TrashCan16" :disabled="!!loading.backup" @click="deleteDevice(d.udid)">{{ $t("settings.delete") }}</NsButton>
                </td>
              </tr>
            </tbody>
          </table>
        </cv-tile>
      </cv-column>
    </cv-row>

    <!-- Restore modal -->
    <NsModal kind="danger" size="default" :visible="restore.show" :isLoading="restore.loading" :primaryButtonDisabled="!canRestore || restore.loading" @modal-hidden="closeRestore" @primary-click="doRestore">
      <template slot="title">{{ $t("settings.restore_title", { name: restore.sourceName }) }}</template>
      <template slot="content">
        <NsInlineNotification kind="warning" :title="$t('settings.restore_warn_title')" :description="$t('settings.restore_warn')" :showCloseButton="false" />
        <div v-if="restore.error" class="field">
          <NsInlineNotification kind="error" :title="$t('action.restore-backup')" :description="restore.error" :showCloseButton="false" />
        </div>

        <h5 class="section-first">{{ $t("settings.restore_pick_snapshot") }}</h5>
        <p v-if="restore.loadingSnapshots" class="bx--form__helper-text">{{ $t("common.processing") }}</p>
        <p v-else-if="!restore.snapshots.length" class="bx--form__helper-text">{{ $t("settings.restore_no_snapshots") }}</p>
        <cv-radio-group v-else vertical>
          <cv-radio-button v-for="s in restore.snapshots" :key="s.snapshot" :label="snapshotLabel(s)" :value="s.snapshot" v-model="restore.snapshot" name="snap" />
        </cv-radio-group>

        <h5 class="section">{{ $t("settings.restore_target") }}</h5>
        <cv-radio-group vertical>
          <cv-radio-button :label="$t('settings.restore_same', { name: restore.sourceName })" value="same" v-model="restore.targetChoice" name="tgt" />
          <cv-radio-button :label="$t('settings.restore_other')" value="other" v-model="restore.targetChoice" name="tgt" :disabled="!otherDevices.length" />
        </cv-radio-group>
        <select v-if="restore.targetChoice === 'other'" class="mode-select field" v-model="restore.targetUdid">
          <option value="">{{ $t("settings.restore_choose_device") }}</option>
          <option v-for="d in otherDevices" :key="d.udid" :value="d.udid">{{ d.name }} ({{ d.ip || "—" }})</option>
        </select>

        <p class="restore-confirm">{{ $t("settings.restore_confirm_hint") }}</p>
      </template>
      <template slot="secondary-button">{{ $t("settings.cancel") }}</template>
      <template slot="primary-button">{{ $t("settings.restore_now") }}</template>
    </NsModal>
  </cv-grid>
</template>

<script>
import to from "await-to-js";
import { mapState } from "vuex";
import { QueryParamService, UtilService, TaskService, IconService, PageTitleService } from "@nethserver/ns8-ui-lib";
import Upload20 from "@carbon/icons-vue/es/upload/20";
import Backup16 from "@carbon/icons-vue/es/data-backup/16";
import Restore16 from "@carbon/icons-vue/es/mobile--download/16";
import TrashCan16 from "@carbon/icons-vue/es/trash-can/16";

export default {
  name: "Settings",
  mixins: [TaskService, IconService, UtilService, QueryParamService, PageTitleService],
  pageTitle() {
    return this.$t("settings.title") + " - " + this.appName;
  },
  data() {
    return {
      q: { page: "settings" },
      urlCheckInterval: null,
      Upload20,
      Backup16,
      Restore16,
      TrashCan16,
      retention: 3,
      include_backups: false,
      container_running: false,
      devices: [],
      upload: { name: "", ip: "", encryption_password: "", content: "", filename: "" },
      restore: {
        show: false, sourceUdid: "", sourceName: "", snapshots: [], loadingSnapshots: false,
        snapshot: "", targetChoice: "same", targetUdid: "", loading: false, error: "",
      },
      loading: { getConfiguration: false, configureModule: false, uploadPairing: false, backup: "", mode: "" },
      error: {
        getConfiguration: "", configureModule: "", uploadPairing: "", runBackup: "", upload_ip: "",
      },
    };
  },
  computed: {
    ...mapState(["instanceName", "core", "appName"]),
    busy() {
      return this.loading.getConfiguration || this.loading.configureModule;
    },
    uploadBusy() {
      return this.loading.uploadPairing;
    },
    otherDevices() {
      return this.devices.filter((d) => d.udid !== this.restore.sourceUdid);
    },
    canRestore() {
      if (!this.restore.snapshot) return false;
      if (this.restore.targetChoice === "other") return !!this.restore.targetUdid;
      return true;
    },
  },
  beforeRouteEnter(to, from, next) {
    next((vm) => {
      vm.watchQueryData(vm);
      vm.urlCheckInterval = vm.initUrlBindingForApp(vm, vm.q.page);
    });
  },
  beforeRouteLeave(to, from, next) {
    clearInterval(this.urlCheckInterval);
    next();
  },
  created() {
    this.getConfiguration();
  },
  methods: {
    formatTime(epoch) {
      if (!epoch) return "—";
      try {
        return new Date(epoch * 1000).toLocaleString();
      } catch (e) {
        return String(epoch);
      }
    },
    snapshotLabel(s) {
      const size = s.size_mb ? ` — ${(s.size_mb / 1024).toFixed(1)} GB` : "";
      return s.snapshot + size;
    },
    onFileChange(ev) {
      const file = ev.target.files && ev.target.files[0];
      if (!file) {
        this.upload.content = "";
        this.upload.filename = "";
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        // reader.result is a data URL "data:...;base64,XXXX"
        const comma = String(reader.result).indexOf(",");
        this.upload.content = comma >= 0 ? String(reader.result).slice(comma + 1) : "";
        this.upload.filename = file.name;
        if (!this.upload.name) this.upload.name = file.name.replace(/\.(plist|mobiledevicepairing)$/i, "");
      };
      reader.readAsDataURL(file);
    },
    async getConfiguration() {
      this.loading.getConfiguration = true;
      this.error.getConfiguration = "";
      const taskAction = "get-configuration";
      const eventId = this.getUuid();
      this.core.$root.$once(`${taskAction}-aborted-${eventId}`, this.getConfigurationAborted);
      this.core.$root.$once(`${taskAction}-completed-${eventId}`, this.getConfigurationCompleted);
      const res = await to(this.createModuleTaskForApp(this.instanceName, { action: taskAction, extra: { title: this.$t("action." + taskAction), isNotificationHidden: true, eventId } }));
      const err = res[0];
      if (err) {
        this.error.getConfiguration = this.getErrorMessage(err);
        this.loading.getConfiguration = false;
      }
    },
    getConfigurationAborted(taskResult, taskContext) {
      console.error(`${taskContext.action} aborted`, taskResult);
      this.error.getConfiguration = this.$t("error.generic_error");
      this.loading.getConfiguration = false;
    },
    getConfigurationCompleted(taskContext, taskResult) {
      this.loading.getConfiguration = false;
      const c = taskResult.output;
      this.retention = c.retention || 3;
      this.include_backups = !!c.include_backups;
      this.container_running = !!c.container_running;
      this.devices = c.devices || [];
    },
    async saveOptions() {
      this.loading.configureModule = true;
      const taskAction = "configure-module";
      const eventId = this.getUuid();
      this.core.$root.$once(`${taskAction}-aborted-${eventId}`, () => { this.loading.configureModule = false; });
      this.core.$root.$once(`${taskAction}-completed-${eventId}`, () => { this.loading.configureModule = false; this.getConfiguration(); });
      const res = await to(this.createModuleTaskForApp(this.instanceName, {
        action: taskAction,
        data: { retention: Number(this.retention), include_backups: this.include_backups },
        extra: { title: this.$t("settings.configure_instance", { instance: this.instanceName }), description: this.$t("common.processing"), eventId },
      }));
      if (res[0]) { this.error.configureModule = this.getErrorMessage(res[0]); this.loading.configureModule = false; }
    },
    async updateMode(udid, mode) {
      this.loading.mode = udid;
      const taskAction = "update-device";
      const eventId = this.getUuid();
      this.core.$root.$once(`${taskAction}-aborted-${eventId}`, () => { this.loading.mode = ""; this.getConfiguration(); });
      this.core.$root.$once(`${taskAction}-completed-${eventId}`, () => { this.loading.mode = ""; this.getConfiguration(); });
      const res = await to(this.createModuleTaskForApp(this.instanceName, {
        action: taskAction,
        data: { udid, backup_mode: mode },
        extra: { title: this.$t("action.update-device"), description: this.$t("common.processing"), eventId, isNotificationHidden: true },
      }));
      if (res[0]) { this.loading.mode = ""; this.getConfiguration(); }
    },
    async uploadPairing() {
      this.error.uploadPairing = "";
      if (!this.upload.content) return;
      this.loading.uploadPairing = true;
      const taskAction = "upload-pairing";
      const eventId = this.getUuid();
      this.core.$root.$once(`${taskAction}-aborted-${eventId}`, () => { this.error.uploadPairing = this.$t("error.generic_error"); this.loading.uploadPairing = false; });
      this.core.$root.$once(`${taskAction}-completed-${eventId}`, () => {
        this.loading.uploadPairing = false;
        this.upload = { name: "", ip: "", encryption_password: "", content: "", filename: "" };
        if (this.$refs.pairingFile) this.$refs.pairingFile.value = "";
        this.getConfiguration();
      });
      const res = await to(this.createModuleTaskForApp(this.instanceName, {
        action: taskAction,
        data: { content: this.upload.content, filename: this.upload.filename, name: this.upload.name, ip: this.upload.ip, encryption_password: this.upload.encryption_password },
        extra: { title: this.$t("action.upload-pairing"), description: this.$t("common.processing"), eventId },
      }));
      if (res[0]) { this.error.uploadPairing = this.getErrorMessage(res[0]); this.loading.uploadPairing = false; }
    },
    async runBackup(udid) {
      this.error.runBackup = "";
      this.loading.backup = udid;
      const taskAction = "run-backup";
      const eventId = this.getUuid();
      this.core.$root.$once(`${taskAction}-aborted-${eventId}`, () => { this.error.runBackup = this.$t("error.generic_error"); this.loading.backup = ""; });
      this.core.$root.$once(`${taskAction}-completed-${eventId}`, () => { this.loading.backup = ""; this.getConfiguration(); });
      const res = await to(this.createModuleTaskForApp(this.instanceName, {
        action: taskAction,
        data: { udid },
        extra: { title: this.$t("action.run-backup"), description: this.$t("common.processing"), eventId },
      }));
      if (res[0]) { this.error.runBackup = this.getErrorMessage(res[0]); this.loading.backup = ""; }
    },
    async deleteDevice(udid) {
      const taskAction = "delete-device";
      const eventId = this.getUuid();
      this.core.$root.$once(`${taskAction}-completed-${eventId}`, () => { this.getConfiguration(); });
      await to(this.createModuleTaskForApp(this.instanceName, {
        action: taskAction,
        data: { udid, delete_backups: false },
        extra: { title: this.$t("action.delete-device"), description: this.$t("common.processing"), eventId },
      }));
    },
    openRestore(d) {
      this.restore = {
        show: true, sourceUdid: d.udid, sourceName: d.name, snapshots: [], loadingSnapshots: true,
        snapshot: "", targetChoice: "same", targetUdid: "", loading: false, error: "",
      };
      this.loadSnapshots(d.udid);
    },
    closeRestore() {
      this.restore.show = false;
    },
    async loadSnapshots(udid) {
      const taskAction = "list-backups";
      const eventId = this.getUuid();
      this.core.$root.$once(`${taskAction}-aborted-${eventId}`, () => { this.restore.loadingSnapshots = false; this.restore.error = this.$t("error.generic_error"); });
      this.core.$root.$once(`${taskAction}-completed-${eventId}`, (ctx, res) => {
        this.restore.loadingSnapshots = false;
        this.restore.snapshots = (res.output && res.output.backups) || [];
      });
      const r = await to(this.createModuleTaskForApp(this.instanceName, {
        action: taskAction,
        data: { udid },
        extra: { title: this.$t("action.list-backups"), isNotificationHidden: true, eventId },
      }));
      if (r[0]) { this.restore.loadingSnapshots = false; this.restore.error = this.getErrorMessage(r[0]); }
    },
    async doRestore() {
      if (!this.canRestore) return;
      this.restore.loading = true;
      this.restore.error = "";
      const target_udid = this.restore.targetChoice === "other" ? this.restore.targetUdid : this.restore.sourceUdid;
      const taskAction = "restore-backup";
      const eventId = this.getUuid();
      this.core.$root.$once(`${taskAction}-aborted-${eventId}`, () => { this.restore.loading = false; this.restore.error = this.$t("error.generic_error"); });
      this.core.$root.$once(`${taskAction}-validation-failed-${eventId}`, () => { this.restore.loading = false; this.restore.error = this.$t("error.generic_error"); });
      this.core.$root.$once(`${taskAction}-completed-${eventId}`, () => { this.restore.loading = false; this.restore.show = false; this.getConfiguration(); });
      const res = await to(this.createModuleTaskForApp(this.instanceName, {
        action: taskAction,
        data: { udid: this.restore.sourceUdid, snapshot: this.restore.snapshot, target_udid },
        extra: { title: this.$t("action.restore-backup"), description: this.$t("common.processing"), eventId },
      }));
      if (res[0]) { this.restore.loading = false; this.restore.error = this.getErrorMessage(res[0]); }
    },
  },
};
</script>

<style scoped lang="scss">
@import "../styles/carbon-utils";
.field { margin-top: $spacing-06; }
.info-tile { margin-top: $spacing-06; }
.section-first { margin-bottom: $spacing-03; }
.section { margin-top: $spacing-06; margin-bottom: $spacing-03; }
.steps { margin: $spacing-03 0 0 $spacing-05; }
.steps li { margin-bottom: $spacing-03; }
.devices { width: 100%; border-collapse: collapse; margin-top: $spacing-05; }
.devices th, .devices td { text-align: left; padding: $spacing-03 $spacing-04; border-bottom: 1px solid #e0e0e0; vertical-align: top; }
.devices .udid { font-family: monospace; font-size: 0.75rem; color: #6f6f6f; }
.devices .actions { white-space: nowrap; }
.devices .actions .bx--btn { margin-right: $spacing-03; }
.mode-select { padding: 2px 6px; min-height: 2rem; }
.restore-confirm { margin-top: $spacing-05; font-size: 0.875rem; color: #6f6f6f; }
.ok { color: #24a148; font-weight: 600; }
.bad { color: #da1e28; font-weight: 600; }
.status { font-size: 0.75rem; }
</style>
