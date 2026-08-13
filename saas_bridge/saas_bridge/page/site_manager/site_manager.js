frappe.provide("saas_bridge");

frappe.pages["site-manager"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Site Manager"),
		single_column: true,
	});

	wrapper.saas_bridge = new saas_bridge.SiteManager(page, wrapper);
};

frappe.pages["site-manager"].on_page_show = function (wrapper) {
	// polling pauses itself while the page is hidden, so coming back has to restart it
	wrapper.saas_bridge && wrapper.saas_bridge.resume_polling();
};

saas_bridge.SiteManager = class SiteManager {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.sites = [];
		this.available_apps = [];
		this.setup();
	}

	async setup() {
		this.page.main.addClass("site-manager-page");
		this.page.set_secondary_action(__("Refresh"), () => this.load_sites(), "refresh");

		// the lists are fetched before the forms are built, so the fields are populated from
		// the start and only a later refresh has to push new options into a live field
		await Promise.all([this.load_apps(), this.load_sites()]);

		this.render_create_section();
		this.render_language_section();
	}

	// --- data ---------------------------------------------------------------

	async load_apps() {
		const r = await frappe.call("saas_bridge.api.get_available_apps");
		this.available_apps = (r && r.message) || [];
	}

	async load_sites() {
		const r = await frappe.call("saas_bridge.api.get_sites");
		this.sites = (r && r.message) || [];

		// the field is built once, so an added site has to be pushed into it by hand
		const field = this.language_form && this.language_form.get_field("site");
		if (field) {
			field.df.options = this.sites;
			field.set_data && field.set_data(this.sites);
		}
	}

	// --- layout -------------------------------------------------------------

	card(title, description) {
		const $card = $(`
			<div class="frappe-card p-4 mb-4">
				<h5 class="mb-1">${frappe.utils.escape_html(title)}</h5>
				<p class="text-muted mb-4">${frappe.utils.escape_html(description)}</p>
				<div class="form-area"></div>
				<div class="action-area mt-3"></div>
				<div class="result-area"></div>
			</div>
		`).appendTo(this.page.main);

		return {
			form: $card.find(".form-area"),
			action: $card.find(".action-area"),
			result: $card.find(".result-area"),
		};
	}

	render_create_section() {
		const areas = this.card(
			__("Create a site"),
			__(
				"Creates a site on this bench with the apps you pick, then adds a System Manager login to it. The install runs in the background — its progress appears below."
			)
		);

		this.create_form = new frappe.ui.FieldGroup({
			body: areas.form[0],
			fields: [
				{
					fieldname: "site",
					fieldtype: "Data",
					label: __("Site name"),
					reqd: 1,
					description: __("Full name, domain included — for example client1.example.com"),
				},
				{
					fieldname: "apps",
					fieldtype: "MultiSelectPills",
					label: __("Apps"),
					get_data: (txt) =>
						this.available_apps.filter((app) => !txt || app.includes(txt.toLowerCase())),
				},
				{
					fieldname: "admin_password",
					fieldtype: "Password",
					label: __("Administrator password"),
					description: __("Generated and shown once if left empty"),
				},
				{ fieldtype: "Column Break" },
				{
					fieldname: "email",
					fieldtype: "Data",
					options: "Email",
					label: __("Login email"),
					description: __("Optional — added to the new site as a System Manager"),
				},
				{
					fieldname: "password",
					fieldtype: "Password",
					label: __("Login password"),
					description: __("Generated and shown once if left empty"),
				},
				{ fieldname: "first_name", fieldtype: "Data", label: __("First name") },
				{ fieldname: "last_name", fieldtype: "Data", label: __("Last name") },
			],
		});
		this.create_form.make();

		this.$create_button = $(`<button class="btn btn-primary btn-sm">${__("Create site")}</button>`)
			.appendTo(areas.action)
			.on("click", () => this.create_site());

		this.$create_result = areas.result;
	}

	render_language_section() {
		const areas = this.card(
			__("Site language"),
			__(
				"Enables a language on a site that already exists. Russian is preselected; the site must already have that language record."
			)
		);

		this.language_form = new frappe.ui.FieldGroup({
			body: areas.form[0],
			fields: [
				{
					fieldname: "site",
					fieldtype: "Autocomplete",
					label: __("Site"),
					reqd: 1,
					options: this.sites,
				},
				{ fieldtype: "Column Break" },
				{
					fieldname: "language",
					fieldtype: "Link",
					options: "Language",
					label: __("Language"),
					reqd: 1,
					default: "ru",
					description: __("Codes come from this site — the target site must have the same one"),
				},
				{ fieldname: "enabled", fieldtype: "Check", label: __("Enabled"), default: 1 },
			],
		});
		this.language_form.make();
		this.language_form.set_value("language", "ru");
		this.language_form.set_value("enabled", 1);

		this.$language_button = $(`<button class="btn btn-primary btn-sm">${__("Apply")}</button>`)
			.appendTo(areas.action)
			.on("click", () => this.set_language());

		this.$language_result = areas.result;
	}

	// --- actions ------------------------------------------------------------

	async create_site() {
		const values = this.create_form.get_values();
		if (!values) return;

		// an empty password field must not reach the API as "", which would fail validation
		// instead of asking for one to be generated
		const args = {};
		for (const [key, value] of Object.entries(values)) {
			if (value !== "" && value !== undefined && value !== null) args[key] = value;
		}

		this.$create_button.prop("disabled", true);
		let response;
		try {
			response = await frappe.call({ method: "saas_bridge.api.create_site", args });
		} catch (e) {
			// a rejected frappe.call has already put the server message on screen; swallowing
			// it here is what keeps it from surfacing again as an unhandled rejection
			return;
		} finally {
			this.$create_button.prop("disabled", false);
		}

		const result = response && response.message;
		if (!result) return;

		this.create_form.set_value("admin_password", "");
		this.create_form.set_value("password", "");
		this.render_generated_passwords(result);
		this.start_polling(result.site);
	}

	async set_language() {
		const values = this.language_form.get_values();
		if (!values) return;

		this.$language_button.prop("disabled", true);
		let response;
		try {
			response = await frappe.call({
				method: "saas_bridge.api.set_site_language",
				args: {
					site: values.site,
					language: values.language,
					enabled: cint(values.enabled),
				},
			});
		} catch (e) {
			return;
		} finally {
			this.$language_button.prop("disabled", false);
		}

		const result = response && response.message;
		if (!result) return;

		const message = result.enabled
			? __("{0} is now enabled on {1}", [result.language, result.site])
			: __("{0} is now disabled on {1}", [result.language, result.site]);
		frappe.show_alert({ message: message, indicator: "green" });
		this.$language_result.html(
			`<div class="mt-3 text-muted">${frappe.utils.escape_html(message)}</div>`
		);
	}

	// --- provisioning progress ----------------------------------------------

	render_generated_passwords(result) {
		const generated = [
			["Administrator", result.admin_password],
			[result.login, result.password],
		].filter(([, secret]) => secret);

		if (!generated.length) {
			this.$create_result.empty();
			return;
		}

		const rows = generated
			.map(
				([user, secret]) =>
					`<div><b>${frappe.utils.escape_html(user)}</b>: <code>${frappe.utils.escape_html(
						secret
					)}</code></div>`
			)
			.join("");

		// the API returns these once and stores them nowhere, so they cannot be recovered
		// by reloading the page — say so rather than let them look like a status line
		this.$create_result.html(`
			<div class="mt-4 p-3" style="border: 1px solid var(--yellow-300); border-radius: var(--border-radius);">
				<div class="mb-2"><b>${__("Generated passwords — copy them now")}</b></div>
				${rows}
				<div class="text-muted mt-2">${__("They are not stored anywhere and cannot be shown again.")}</div>
			</div>
		`);
	}

	start_polling(site) {
		this.stop_polling();
		this.polled_site = site;

		this.$status = $(`<div class="mt-4"></div>`).appendTo(this.$create_result);
		this.tick();
	}

	stop_polling() {
		if (this.poll_timer) clearTimeout(this.poll_timer);
		this.poll_timer = null;
	}

	resume_polling() {
		if (this.polled_site && !this.poll_timer) this.tick();
	}

	async tick() {
		this.stop_polling();

		// nothing to show while the page is off screen, and a dead tab should not keep
		// asking the bench for status
		if (!$(this.wrapper).is(":visible")) {
			this.poll_timer = setTimeout(() => this.tick(), 4000);
			return;
		}

		const site = this.polled_site;
		let response;
		try {
			response = await frappe.call({
				method: "saas_bridge.api.get_site_status",
				args: { site: site },
				no_spinner: true,
			});
		} catch (e) {
			// frappe has already shown the error — stop rather than repeat it every 4s, but
			// leave a mark, otherwise the progress block just sits there empty
			this.polled_site = null;
			this.$status.html(
				`<div class="text-muted">${__("Progress of {0} is no longer available — the run state is kept for 24 hours.", [frappe.utils.escape_html(site)])}</div>`
			);
			return;
		}

		const state = response && response.message;
		if (!state) {
			this.polled_site = null;
			return;
		}

		this.render_status(state);

		if (["queued", "running"].includes(state.status)) {
			this.poll_timer = setTimeout(() => this.tick(), 4000);
			return;
		}

		this.polled_site = null;

		// a finished run is the moment the site becomes something the language form can act
		// on, so pull it into that field without waiting for a manual refresh
		if (state.status === "success") this.load_sites();

		frappe.show_alert({
			message:
				state.status === "success"
					? __("{0} is ready", [state.site])
					: __("Creating {0} failed", [state.site]),
			indicator: state.status === "success" ? "green" : "red",
		});
	}

	render_status(state) {
		const colors = { queued: "orange", running: "blue", success: "green", failed: "red" };
		const line = (label, value) =>
			value
				? `<div class="text-muted">${frappe.utils.escape_html(label)}: ${frappe.utils.escape_html(
						String(value)
					)}</div>`
				: "";

		this.$status.html(`
			<div class="mb-2">
				<span class="indicator-pill ${colors[state.status] || "gray"}">
					${frappe.utils.escape_html(state.site)} — ${frappe.utils.escape_html(state.status)}
				</span>
			</div>
			${line(__("Apps installed"), (state.apps_installed || []).join(", "))}
			${line(__("Login created"), state.login_created)}
			${line(__("Cleanup"), state.cleanup)}
			${state.error ? `<div class="mt-2"><pre class="small">${frappe.utils.escape_html(state.error)}</pre></div>` : ""}
		`);
	}
};
