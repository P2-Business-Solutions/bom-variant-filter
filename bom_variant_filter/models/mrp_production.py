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
        Re-pick ``bom_id`` when the BOM super() kept doesn't cover the variant.

        Odoo core's ``mrp.production._compute_bom_id`` only reassigns ``bom_id``
        when the current BOM is empty, its ``product_tmpl_id`` no longer matches
        the MO's template, or it is variant-specific and points at a different
        variant. A template-level BOM (``product_id=False``) whose
        ``product_tmpl_id`` matches is always kept — even when the user has just
        switched to a variant the BOM can't actually build.

        That is exactly the case this module exists to fix. Consider a
        *Precut* BOM (sequence=0) whose lens lines are all ``Lens Material =
        Glass`` and a *Raw* BOM (sequence=1) that carries both Glass and PC
        lens lines. When an MO is first created for a Glass variant, core's
        ``_bom_find`` (via our override) picks the Precut BOM and assigns it.
        When the user then edits the MO and switches the variant to a PC
        lens, ``_compute_bom_id`` reruns but keeps the Precut BOM because it
        is template-level and matches the template. ``_compute_move_raw_ids``
        then re-explodes the Precut BOM for the PC variant,
        ``_skip_bom_line`` silently drops every lens line (all Glass-only),
        and the resulting MO is missing its lens component while still
        referencing the wrong BOM.

        Our ``mrp.bom._bom_find`` override already knows how to pick the Raw
        BOM for the PC variant — but core's compute throws that result away
        for the "template BOM still matches" branch. We therefore let super()
        run its normal logic, then detect the MOs whose resulting ``bom_id``
        has no applicable lines for the current variant and rerun
        ``_bom_find`` to swap the BOM in place. ``_compute_move_raw_ids``
        will re-fire on the reassignment via its own ``bom_id`` dependency,
        producing a correct component list.

        MOs for which no alternative BOM qualifies are left alone: downgrading
        ``bom_id`` to ``False`` would be more disruptive than leaving the
        broken selection visible, and the user can still fix it manually.
        """
        super()._compute_bom_id()

        MrpBom = self.env['mrp.bom']
        needs_reassignment = defaultdict(lambda: self.env['mrp.production'])
        for mo in self:
            if not mo.product_id or not mo.bom_id:
                continue
            if MrpBom._bom_has_applicable_lines(mo.bom_id, mo.product_id):
                continue
            needs_reassignment[mo.company_id.id] |= mo

        if not needs_reassignment:
            return

        picking_type_id = self._context.get('default_picking_type_id')
        picking_type = (
            picking_type_id
            and self.env['stock.picking.type'].browse(picking_type_id)
        )

        for company_id, productions in needs_reassignment.items():
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
