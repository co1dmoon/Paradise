/obj/effect/proc_holder/spell/morph_spell/open_vent
	name = "Open Vents"
	desc = "Spit out acidic puke on nearby vents or scrubbers. Will take a little while for the acid to take effect. Not usable from inside a vent."
	action_icon_state = "acid_vent"
	base_cooldown = 10 SECONDS
	hunger_cost = 10


/obj/effect/proc_holder/spell/morph_spell/open_vent/create_new_targeting()
	var/datum/spell_targeting/aoe/T = new
	T.range = 1
	T.allowed_type = /obj/machinery/atmospherics/unary
	return T


/obj/effect/proc_holder/spell/morph_spell/open_vent/valid_target(target, user)
	if(istype(target, /obj/machinery/atmospherics/unary/vent_scrubber))
		var/obj/machinery/atmospherics/unary/vent_scrubber/S = target
		return S.welded
	if(istype(target, /obj/machinery/atmospherics/unary/vent_pump))
		var/obj/machinery/atmospherics/unary/vent_pump/V = target
		return V.welded
	return FALSE


/obj/effect/proc_holder/spell/morph_spell/open_vent/cast(list/targets, mob/user)
	if(!length(targets))
		to_chat(user, "<span class='warning'>No nearby welded vents found!</span>")
		revert_cast(user)
		return
	to_chat(user, "<span class='sinister'>You begin regurgitating up some acidic puke!</span>")
	if(!do_after(user, 2 SECONDS, FALSE, user))
		to_chat(user, "<span class='warning'>You swallow the acid again.</span>")
		revert_cast(user)
		return
	for(var/thing in targets)
		var/obj/machinery/atmospherics/unary/unary = thing
		unary.add_overlay(GLOB.acid_overlay)
		addtimer(CALLBACK(src, PROC_REF(unweld_vent), unary), 2 SECONDS)
		playsound(unary, 'sound/items/welder.ogg', 100, TRUE)


/obj/effect/proc_holder/spell/morph_spell/open_vent/proc/unweld_vent(obj/machinery/atmospherics/unary/unary)
	if(istype(unary, /obj/machinery/atmospherics/unary/vent_scrubber))
		var/obj/machinery/atmospherics/unary/vent_scrubber/scrubber = unary
		scrubber.welded = FALSE
	else if(istype(unary, /obj/machinery/atmospherics/unary/vent_pump))
		var/obj/machinery/atmospherics/unary/vent_scrubber/vent = unary
		vent.welded = FALSE
	unary.update_icon()
	unary.cut_overlay(GLOB.acid_overlay)

