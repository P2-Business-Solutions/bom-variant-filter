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
        Return True if at least one component line on ``bom`` applies to
        ``product``.

        Delegates to Odoo's own ``mrp.bom.line._skip_bom_line()``, which runs
        the official variant-matching logic (``_skip_for_no_variant``) over
        the line's ``bom_product_template_attribute_value_ids`` and correctly
        handles ``always``, ``dynamic``, and ``no_variant`` attribute kinds.
        That method is pure attribute-value comparison — it issues no search
        and requires no MO context — so it is safe to call from ``_bom_find``.

        A BOM with no lines returns False so it won't silently win over a
        better candidate.
        """
        if not bom.bom_line_ids:
            return False
        return any(
            not line._skip_bom_line(product) for line in bom.bom_line_ids
        )

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
