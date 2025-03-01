:og:description: Basic commands for working with pipette tips.

.. _pipette-tips:

*************************
Manipulating Pipette Tips
*************************

Your robot needs to attach a disposable tip to the pipette before it can aspirate or dispense liquids. The API provides three basic functions that help the robot attach and manage pipette tips during a protocol run. These methods are :py:meth:`.InstrumentContext.pick_up_tip`, :py:meth:`.InstrumentContext.drop_tip`, and :py:meth:`.InstrumentContext.return_tip`. Respectively, these methods tell the robot to pick up a tip from a tip rack, drop a tip into the trash (or another location), and return a tip to its location in the tip rack.

The following sections demonstrate how to use each method and include sample code. The examples used here assume that you've loaded the pipettes and labware from the basic :ref:`protocol template <protocol-template>`.

.. _basic-tip-pickup:

Picking Up a Tip
================

To pick up a tip, call the :py:meth:`~.InstrumentContext.pick_up_tip` method without any arguments::
    
    pipette.pick_up_tip()

When added to the protocol template, this simple statement works because the API knows which tip rack is associated with ``pipette``, as indicated by ``tip_racks=[tiprack_1]`` in the :py:meth:`.load_instrument` call. And it knows the on-deck location of the tip rack (slot D3 on Flex, slot 3 on OT-2) from the ``location`` argument of :py:meth:`~.ProtocolContext.load_labware`. Given this information, the robot moves to the tip rack and picks up a tip from position A1 in the rack. On subsequent calls to ``pick_up_tip()``, the robot will use the next available tip. For example::

    pipette.pick_up_tip()  # picks up tip from rack location A1
    pipette.drop_tip()     # drops tip in trash bin
    pipette.pick_up_tip()  # picks up tip from rack location B1
    pipette.drop_tip()     # drops tip in trash bin 

If you omit the ``tip_rack`` argument from the ``pipette`` variable, the API will raise an error. In that case, you must pass the tip rack's location to ``pick_up_tip`` like this::
    
    pipette.pick_up_tip(tiprack_1["A1"])
    pipette.drop_tip()
    pipette.pick_up_tip(tiprack_1["B1"]) 

In most cases, it's best to associate tip racks with a pipette and let the API automatically track pickup location for you. This also makes it easy to pick up tips when iterating over a loop, as shown in the next section.

.. versionadded:: 2.0

Automating Tip Pick Up
======================

When used with Python's :py:class:`range` class, a ``for`` loop brings automation to the tip pickup and tracking process. It also eliminates the need to call ``pick_up_tip()`` multiple times. For example, this snippet tells the robot to sequentially use all the tips in a 96-tip rack::

    for i in range(96):
        pipette.pick_up_tip()
        # liquid handling commands
        pipette.drop_tip()

If your protocol requires a lot of tips, add a second tip rack to the protocol. Then, associate it with your pipette and increase the number of repetitions in the loop. The robot will work through both racks. 

First, add another tip rack to the sample protocol::

    tiprack_2 = protocol.load_labware(
        load_name="opentrons_flex_96_tiprack_1000ul",
        location="C3"
    )

Next, change the pipette's ``tip_rack`` property to include the additional rack::

    pipette = protocol.load_instrument(
        instrument_name="flex_1channel_1000",
        mount="left",
        tip_racks=[tiprack_1, tiprack_2],
    )

Finally, iterate over a larger range::

    for i in range(192):
        pipette.pick_up_tip()
        # liquid handling commands
        pipette.drop_tip()

For a more advanced "real-world" example, review the :ref:`off-deck location protocol <off-deck-location>` on the :ref:`moving-labware` page. This example also uses a ``for`` loop to iterate through a tip rack, but it includes other commands that pause the protocol and let you replace an on-deck tip rack with another rack stored in an off-deck location.

.. _pipette-drop-tip:

Dropping a Tip
==============

To drop a tip in the pipette's trash container, call the :py:meth:`~.InstrumentContext.drop_tip` method with no arguments::
    
    pipette.pick_up_tip()

You can specify where to drop the tip by passing in a location. For example, this code drops a tip in the trash bin and returns another tip to to a previously used well in a tip rack::

    pipette.pick_up_tip()            # picks up tip from rack location A1
    pipette.drop_tip()               # drops tip in default trash container
    pipette.pick_up_tip()            # picks up tip from rack location B1
    pipette.drop_tip(tiprack["A1"])  # drops tip in rack location A1

.. versionadded:: 2.0

Another use of the ``location`` parameter is to drop a tip in a specific trash container. For example, calling ``pipette.drop_tip(chute)`` will dispose tips in the waste chute, even if the pipette's default trash container is a trash bin::

    pipette.pick_up_tip()    # picks up tip from rack location A1
    pipette.drop_tip()       # drops tip in default trash container
    pipette.pick_up_tip()    # picks up tip from rack location B1
    pipette.drop_tip(chute)  # drops tip in waste chute

.. versionadded:: 2.16

.. _pipette-return-tip:

Returning a Tip
===============

To return a tip to its original location, call the :py:meth:`~.InstrumentContext.return_tip` method with no arguments::

    pipette.return_tip()

.. versionadded:: 2.0

.. note::
    You can't return tips with a pipette that's configured to use :ref:`partial tip pickup <partial-tip-pickup>`. This restriction ensures that the pipette has clear access to unused tips. For example, a 96-channel pipette in column configuration can't reach column 2 unless column 1 is empty. 

    If you call ``return_tip()`` while using partial tip pickup, the API will raise an error. Use ``drop_tip()`` to dispose the tips instead.

Working With Used Tips
======================

Currently, the API considers tips as "used" after being picked up. For example, if the robot picked up a tip from rack location A1 and then returned it to the same location, it will not attempt to pick up this tip again, unless explicitly specified. Instead, the robot will pick up a tip starting from rack location B1. For example::

    pipette.pick_up_tip()                # picks up tip from rack location A1
    pipette.return_tip()                 # drops tip in rack location A1
    pipette.pick_up_tip()                # picks up tip from rack location B1
    pipette.drop_tip()                   # drops tip in trash bin
    pipette.pick_up_tip(tiprack_1["A1"]) # picks up tip from rack location A1

Early API versions treated returned tips as unused items. They could be picked up again without an explicit argument. For example:: 

    pipette.pick_up_tip()  # picks up tip from rack location A1
    pipette.return_tip()   # drops tip in rack location A1
    pipette.pick_up_tip()  # picks up tip from rack location A1

.. versionchanged:: 2.2
