from collections import defaultdict

from odoo import api, models


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    @api.depends('state', 'move_finished_ids')
    def _compute_show_allocation(self):
        """
        Skip unsaved (NewId) records when computing ``show_allocation``.

        Odoo core's implementation in ``addons/mrp/models/mrp_production.py``
        issues a ``stock.move`` search with the domain term::

            ('raw_material_production_id', '!=', mo.id)

        When ``mo`` has not yet been persisted, ``mo.id`` is a ``NewId`` and
        the ORM's ``_condition_to_sql`` cannot translate the term into SQL,
        producing a spurious warning on every onchange::

            _condition_to_sql: ignored
                ('raw_material_production_id', '!=', NewId),
                did you mean ('raw_material_production_id', 'in', recs.ids)?

        The warning is triggered, for example, every time a user adds a
        product/component line to an unsaved MO in the form view (which
        refires the ``move_finished_ids`` dependency of this compute).

        An unsaved MO cannot yet have any related consumed stock moves, so
        ``show_allocation`` is guaranteed to be ``False`` for NewId records.
        We therefore set the flag directly and only delegate to super() for
        records that are actually persisted, avoiding the offending search
        entirely.
        """
        new_records = self.filtered(lambda mo: isinstance(mo.id, models.NewId))
        new_records.show_allocation = False

        persisted = self - new_records
        if persisted:
            return super(MrpProduction, persisted)._compute_show_allocation()

    def _compute_bom_id(self):
        """
        Re-pick ``bom_id`` when super() kept a template-level BOM that is no
        longer the best choice for the current variant.

        Odoo core's ``mrp.production._compute_bom_id`` only reassigns
        ``bom_id`` when the current BOM is empty, its ``product_tmpl_id`` no
        longer matches the MO's template, or it is variant-specific and
        points at a different variant. A template-level BOM
        (``product_id=False``) whose ``product_tmpl_id`` matches is always
        kept across variant changes — regardless of whether another
        template-level BOM on the same template is now a better fit.

        This is exactly the case this module exists to fix, and the bug is
        symmetric:

          * From Glass to PC lens. Precut BOM 1 (sequence=0) has only
            Glass-only lens lines. The user creates the MO for a Glass
            variant, super assigns BOM 1 via ``_bom_find``, then switches
            the variant to PC. Super keeps BOM 1 (template-level match),
            ``_compute_move_raw_ids`` re-explodes it, ``_skip_bom_line``
            drops every Glass-only lens line, and the MO ends up with no
            lens while still referencing BOM 1 — the original bug.

          * From PC back to Glass. BOM 2 (Raw, sequence=1) carries both
            Glass and PC lens lines, so it is perfectly applicable to a
            Glass variant. But BOM 1 has the lower sequence and should win
            for Glass variants. Super keeps BOM 2, and the MO sticks on the
            lower-priority BOM across the variant switch.

        Both symptoms come from super refusing to re-run ``_bom_find`` on
        variant changes when the current BOM is template-level. We let
        super() run its normal logic, then for every MO whose ``bom_id`` is
        template-level (the branch where super would have kept it) we rerun
        ``_bom_find`` and swap in whatever it returns if it differs from the
        current selection. That covers both directions in a single rule and
        leaves variant-specific BOMs — which super already re-evaluates on
        variant mismatch — alone.

        ``_compute_move_raw_ids`` depends on ``bom_id`` and will re-fire on
        the reassignment, producing a correct component list. MOs for which
        ``_bom_find`` returns an empty recordset are left alone:
        downgrading ``bom_id`` to ``False`` would be more disruptive than
        leaving the current selection visible, and the user can still fix
        it manually.
        """
        super()._compute_bom_id()

        MrpBom = self.env['mrp.bom']
        candidates_by_company = defaultdict(lambda: self.env['mrp.production'])
        for mo in self:
            if not mo.product_id or not mo.bom_id:
                continue
            # Only second-guess super() for the "template-level BOM kept
            # across a variant change" branch. Variant-specific BOMs
            # (``product_id`` set) are already re-evaluated by super's own
            # ``bom_id.product_id != production.product_id`` check, and a
            # manually-chosen variant-specific BOM should not be silently
            # replaced here.
            if mo.bom_id.product_id:
                continue
            if mo.bom_id.product_tmpl_id != mo.product_tmpl_id:
                continue
            candidates_by_company[mo.company_id.id] |= mo

        if not candidates_by_company:
            return

        picking_type_id = self._context.get('default_picking_type_id')
        picking_type = (
            picking_type_id
            and self.env['stock.picking.type'].browse(picking_type_id)
        )

        for company_id, productions in candidates_by_company.items():
            boms_by_product = MrpBom.with_context(active_test=True)._bom_find(
                productions.product_id,
                picking_type=picking_type,
                company_id=company_id,
                bom_type='normal',
            )
            for production in productions:
                new_bom = boms_by_product[production.product_id]
                if new_bom and new_bom != production.bom_id:
                    production.bom_id = new_bom.id
                    # Mirror super()'s behaviour: picking_type_id depends on
                    # bom_id, so make sure the dependent compute re-runs.
                    self.env.add_to_compute(
                        production._fields['picking_type_id'], production
                    )
