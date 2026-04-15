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
        Return True only if the BOM plausibly covers every component "role"
        it defines for ``product``.

        A naive "at least one line applies" check is too permissive for BOMs
        that enumerate variants across an axis — e.g. a *Precut* BOM whose
        lens lines span ``Lens Color ∈ {Blue Mirror, Green Mirror, Violet
        Mirror, Rose Mirror}`` but are all ``Lens Material = Glass``. For a
        PC variant, the frame line and the generic packaging lines still
        apply, so the naive check reports the BOM as applicable even though
        no lens line fits the variant and the resulting MO would be missing
        its lens. ``_bom_find`` never falls through to the Raw BOM that has
        the PC lens.

        A naive "every attribute-axis group must have a matching line" check
        is the other extreme — too strict. It breaks the equally common
        pattern of *optional* variant-specific lines (e.g. a logo sticker
        present only for ``Color = Red``): a Blue variant would then be
        rejected from a BOM that is otherwise perfectly valid for it.

        The rule this method implements distinguishes the two by asking
        whether the lines in each group *enumerate* across the axis:

          * Group lines by the frozenset of attribute *ids* their
            restrictions reference (the "axis signature"). Lines with no
            restriction fall into the empty signature and are always
            applicable generics.
          * A group is treated as a **required role** only when its lines
            collectively cover more than one distinct value of at least one
            attribute in the signature. Enumeration across 2+ values is the
            signal that the author intended the group to span variants, so
            a variant with no matching line in that group is outside the
            BOM's scope.
          * A group whose lines all reference the same single value on every
            axis is treated as an **optional one-off** and cannot disqualify
            the BOM — a variant that doesn't match simply doesn't carry that
            line.

        After all group checks pass, the BOM must still contribute at least
        one line that actually applies to the variant. A BOM with zero
        applicable lines produces an empty MO, which is exactly what this
        module exists to avoid — so such a BOM is rejected even if every
        group was considered optional.

        Line-level applicability is delegated to Odoo's own
        ``mrp.bom.line._skip_bom_line()``, which implements the official
        variant-matching logic (``_skip_for_no_variant``) and handles
        ``always``, ``dynamic``, and ``no_variant`` attribute kinds.

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

        for signature, lines in groups.items():
            if not signature:
                # Unrestricted generics — always applicable, never required.
                continue

            # Determine whether this group enumerates across any axis. If
            # every attribute in the signature is pinned to a single value
            # across all lines in the group, the group is treated as an
            # optional one-off and does not disqualify the BOM.
            values_per_attribute = defaultdict(set)
            for line in lines:
                for ptav in line.bom_product_template_attribute_value_ids:
                    values_per_attribute[ptav.attribute_id.id].add(ptav.id)

            enumerates_multiple = any(
                len(values) > 1 for values in values_per_attribute.values()
            )
            if not enumerates_multiple:
                continue

            if not any(not line._skip_bom_line(product) for line in lines):
                return False

        # Safety net: even if every required-group check passed (or every
        # group was optional), the BOM must contribute at least one line
        # that actually applies to the variant. Otherwise _bom_find would
        # be returning a BOM that produces an empty MO — exactly the case
        # this module was written to prevent.
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
