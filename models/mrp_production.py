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
