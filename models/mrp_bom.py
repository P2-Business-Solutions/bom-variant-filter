from collections import defaultdict

from odoo import models, api
import logging

_logger = logging.getLogger(__name__)


class MrpBom(models.Model):
    _inherit = 'mrp.bom'

    @api.model
    def _bom_find(self, products, picking_type=None, company_id=False, bom_type=False):
        """
        Override _bom_find to skip BOM candidates where no component lines
        apply to the requested product variant.

        Standard Odoo selects the highest-priority (lowest sequence) BOM that
        matches the product template/variant, without checking whether any of
        its component lines actually resolve for the variant. This means a
        precut BOM (sequence=10) would be selected for a PC variant even though
        all its lines are restricted to Glass — resulting in an empty MO.

        This override:
          1. Lets super() pick the normal winner.
          2. If that BOM has zero applicable lines for the variant, searches for
             the next candidate (by sequence) that does have applicable lines.
          3. Returns an empty BOM recordset if no candidate qualifies, rather
             than returning a BOM that would produce a blank manufacturing order.
        """
        result = super()._bom_find(
            products,
            picking_type=picking_type,
            company_id=company_id,
            bom_type=bom_type,
        )

        for product, bom in list(result.items()):
            if not bom:
                continue

            if self._bom_has_applicable_lines(bom, product):
                # Normal case — primary BOM is fine, nothing to do.
                continue

            _logger.debug(
                "BOM '%s' (id=%s) has no applicable lines for variant '%s'. "
                "Searching for fallback BOM.",
                bom.display_name, bom.id, product.display_name,
            )

            fallback = self._find_fallback_bom(
                product, bom, picking_type, company_id, bom_type
            )

            if fallback:
                _logger.debug(
                    "Fallback BOM '%s' (id=%s) selected for variant '%s'.",
                    fallback.display_name, fallback.id, product.display_name,
                )
            else:
                _logger.warning(
                    "No applicable BOM found for variant '%s' — returning empty.",
                    product.display_name,
                )

            result[product] = fallback

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _bom_has_applicable_lines(self, bom, product):
        """
        Return True only if the BOM covers every component "role" it defines
        for ``product``.

        A naive "at least one line applies" check is too permissive for BOMs
        that mix several component roles — e.g. a *Precut* BOM that contains
        variant-restricted lens lines (Glass only), variant-restricted frame
        lines, and generic packaging lines. For a PC variant, the frame line
        and the packaging lines still apply, so the naive check incorrectly
        reports the BOM as applicable even though the BOM has no lens line
        that fits the variant and would produce an MO missing its lens.

        The real requirement is stronger: for every distinct combination of
        attribute *axes* the BOM restricts lines on, at least one line in
        that group must resolve for ``product``. In the example above the
        BOM has three groups:

          * ``{Lens Color, Lens Material}`` — the lens lines.
          * ``{Frame Color}`` — the frame lines.
          * ``frozenset()`` — unrestricted (generic) lines.

        For a PC variant the first group has zero applicable lines, so the
        BOM is reported non-applicable and ``_bom_find`` falls through to
        the next candidate (the Raw BOM, which does have a PC lens line).

        Applicability of individual lines still delegates to Odoo's own
        ``mrp.bom.line._skip_bom_line()``, which implements the official
        variant-matching logic and correctly handles ``always``, ``dynamic``,
        and ``no_variant`` attribute kinds.

        A BOM with no lines returns False so it won't silently win over a
        better candidate.
        """
        if not bom.bom_line_ids:
            return False

        groups = defaultdict(lambda: self.env['mrp.bom.line'])
        for line in bom.bom_line_ids:
            axis_signature = frozenset(
                line.bom_product_template_attribute_value_ids.attribute_id.ids
            )
            groups[axis_signature] |= line

        for lines in groups.values():
            if not any(not line._skip_bom_line(product) for line in lines):
                return False

        return True

    def _find_fallback_bom(self, product, exclude_bom, picking_type, company_id, bom_type):
        """
        Search for the next-best BOM for ``product``, excluding ``exclude_bom``,
        returning the first one (by sequence, then variant-specific, then id)
        that has at least one applicable component line.

        Reuses ``_bom_find_domain`` so that third-party extensions of the base
        domain (e.g. ``mrp_subcontracting``) are respected, and matches the
        native ``_bom_find`` ordering (``sequence, product_id, id``) so that
        variant-specific BOMs are preferred over template-level BOMs at the
        same sequence — the same tiebreak Odoo applies for the primary pick.
        """
        domain = list(self._bom_find_domain(
            product,
            picking_type=picking_type,
            company_id=company_id,
            bom_type=bom_type,
        ))
        domain.append(('id', '!=', exclude_bom.id))

        candidates = self.search(domain, order='sequence, product_id, id')

        for candidate in candidates:
            if self._bom_has_applicable_lines(candidate, product):
                return candidate

        return self.env['mrp.bom']
